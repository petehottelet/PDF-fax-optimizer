#!/usr/bin/env python3
"""Fax conversion pipeline.

Converts PDF pages to fax-native 1-bit (bilevel) output and packs them into a
CCITT-G4 PDF (default) or a Class-F multipage TIFF. Implements the decisions
described in references/fax-optimization.md:

  - anisotropic rasterization at a fax-native resolution (clamped to 1728 px)
  - MRC-lite content segmentation (hard-threshold text, halftone photos),
    using the PDF's own embedded-image rectangles
  - background flatten / despeckle / deskew pre-cleans
  - selectable dithering (floyd, atkinson, ordered, clustered) with `auto`
  - stroke thickening to save hairlines/small fonts
  - lossless CCITT-G4 embedding (no re-encode) via img2pdf
  - per-page transmission-time estimate from the actual G4-encoded size

Designed to be importable (used by optimize_pdf.py) or run directly.
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import cv2
import fitz  # PyMuPDF
from PIL import Image
import img2pdf

# Fax-native resolutions: (horizontal_dpi, vertical_dpi)
RESOLUTIONS = {
    "standard": (204, 98),
    "fine": (204, 196),
    "superfine": (204, 391),
}
MAX_SCANLINE_PX = 1728


@dataclass
class FaxOptions:
    resolution: str = "fine"
    dither: str = "auto"            # auto|floyd|atkinson|ordered|clustered|none
    fax_heavy: bool = False
    segmentation: str = "embedded"  # embedded|variance|none
    thicken: bool = False
    flatten_bg: bool = True
    despeckle: bool = True
    deskew: bool = True
    fmt: str = "pdf"                # pdf|tiff
    max_scanline_px: int = MAX_SCANLINE_PX
    line_rate_bps: int = 14400
    page_overhead_s: float = 1.5
    min_font_px: int = 12           # below this stroke height -> warn / thicken


@dataclass
class PageReport:
    index: int
    encoded_bytes: int = 0
    est_transmission_s: float = 0.0
    photo_regions: int = 0
    photo_fraction: float = 0.0
    dither: str = ""
    already_bilevel: bool = False
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Rasterization                                                               #
# --------------------------------------------------------------------------- #
def render_page_gray(page: fitz.Page, hdpi: int, vdpi: int,
                     max_w: int) -> np.ndarray:
    """Render a page to a grayscale ndarray at anisotropic dpi.

    PyMuPDF's Matrix lets us scale x and y independently, so we render straight
    onto the fax pixel grid instead of resampling a square render (which would
    distort the page and risk moire).
    """
    # points are 1/72 inch; scale = dpi / 72 per axis
    sx, sy = hdpi / 72.0, vdpi / 72.0
    # clamp horizontal scale so width never exceeds max_w
    page_w_pt = page.rect.width
    if page_w_pt * sx > max_w:
        sx = max_w / page_w_pt
    mat = fitz.Matrix(sx, sy)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return arr.copy()


def photo_region_mask(page: fitz.Page, shape, hdpi, vdpi, max_w,
                      mode: str) -> np.ndarray:
    """Boolean mask (True = continuous-tone/photo) for MRC routing.

    `embedded` uses the PDF's embedded-image rectangles (robust, structural).
    `variance` uses a local-variance heuristic for flattened scans with no
    image structure. `none` returns an all-False mask (whole page thresholded).
    """
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    if mode == "none":
        return mask

    if mode == "embedded":
        sx, sy = hdpi / 72.0, vdpi / 72.0
        if page.rect.width * sx > max_w:
            sx = max_w / page.rect.width
        try:
            imgs = page.get_images(full=True)
        except Exception:
            imgs = []
        for img in imgs:
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for r in rects:
                x0 = max(0, int(r.x0 * sx)); y0 = max(0, int(r.y0 * sy))
                x1 = min(w, int(r.x1 * sx)); y1 = min(h, int(r.y1 * sy))
                if x1 > x0 and y1 > y0:
                    mask[y0:y1, x0:x1] = True
    # If 'embedded' found nothing, the caller (process_page) falls back to the
    # variance heuristic. This function only handles the structural path.
    return mask


def variance_photo_mask(gray: np.ndarray, block: int = 24,
                        var_lo: float = 80.0, var_hi: float = 4000.0) -> np.ndarray:
    """Heuristic photo mask for flattened scans.

    Text/line-art blocks are bimodal (very high variance, sparse); flat
    background is near-zero variance; continuous-tone photo blocks fall in a
    mid band. We mark mid-variance blocks and clean up with morphology.
    """
    h, w = gray.shape
    gf = gray.astype(np.float32)
    mean = cv2.boxFilter(gf, -1, (block, block))
    sq = cv2.boxFilter(gf * gf, -1, (block, block))
    var = np.clip(sq - mean * mean, 0, None)
    band = ((var > var_lo) & (var < var_hi)).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (block, block))
    band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, k)
    band = cv2.morphologyEx(band, cv2.MORPH_OPEN, k)
    return band.astype(bool)


# --------------------------------------------------------------------------- #
# Pre-cleaning                                                                #
# --------------------------------------------------------------------------- #
def flatten_background(gray: np.ndarray, knee: int = 200) -> np.ndarray:
    """Push near-white pixels to pure white so faint content survives threshold,
    and gently stretch contrast below the knee."""
    out = gray.astype(np.float32)
    out[out >= knee] = 255.0
    below = out < knee
    out[below] = np.clip(out[below] * (255.0 / knee), 0, 255)
    return out.astype(np.uint8)


def deskew_gray(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Estimate small skew from dark-pixel orientation and rotate to correct it."""
    inv = 255 - gray
    coords = np.column_stack(np.where(inv > 64))
    if coords.shape[0] < 50:
        return gray, 0.0
    angle = cv2.minAreaRect(coords[:, ::-1].astype(np.float32))[-1]
    if angle < -45:
        angle += 90
    if abs(angle) < 0.2 or abs(angle) > 15:
        return gray, 0.0
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rot = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    return rot, float(angle)


def despeckle_bw(bw: np.ndarray, min_area: int = 2) -> np.ndarray:
    """Remove isolated black specks (connected black components <= min_area px).
    bw: uint8 0/255 where 0 = black."""
    black = (bw == 0).astype(np.uint8)
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(black, connectivity=8)
    out = bw.copy()
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] <= min_area:
            out[lbl == i] = 255
    return out


def thicken_bw(bw: np.ndarray) -> np.ndarray:
    """Dilate black features by one pixel so hairlines survive transmission."""
    black = (bw == 0).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    black = cv2.dilate(black, k, iterations=1)
    return np.where(black > 0, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Halftoning / thresholding                                                   #
# --------------------------------------------------------------------------- #
def threshold_otsu(gray: np.ndarray) -> np.ndarray:
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


def dither_floyd(gray: np.ndarray) -> np.ndarray:
    """Floyd-Steinberg via Pillow's fast C implementation."""
    return np.asarray(Image.fromarray(gray, "L").convert("1")).astype(np.uint8) * 255


# Error-diffusion kernels as (divisor, [(dy, dx, weight), ...]). Atkinson
# deliberately diffuses only 6/8 of the error (cleaner whites, less speckle).
ED_KERNELS = {
    "atkinson": (8, [(0, 1, 1), (0, 2, 1),
                     (1, -1, 1), (1, 0, 1), (1, 1, 1),
                     (2, 0, 1)]),
    "jarvis": (48, [(0, 1, 7), (0, 2, 5),
                    (1, -2, 3), (1, -1, 5), (1, 0, 7), (1, 1, 5), (1, 2, 3),
                    (2, -2, 1), (2, -1, 3), (2, 0, 5), (2, 1, 3), (2, 2, 1)]),
    "stucki": (42, [(0, 1, 8), (0, 2, 4),
                    (1, -2, 2), (1, -1, 4), (1, 0, 8), (1, 1, 4), (1, 2, 2),
                    (2, -2, 1), (2, -1, 2), (2, 0, 4), (2, 1, 2), (2, 2, 1)]),
    "sierra": (32, [(0, 1, 5), (0, 2, 3),
                    (1, -2, 2), (1, -1, 4), (1, 0, 5), (1, 1, 4), (1, 2, 2),
                    (2, -1, 2), (2, 0, 3), (2, 1, 2)]),
}


def error_diffuse(gray: np.ndarray, kernel: str) -> np.ndarray:
    """Generic serpentine error diffusion for the named kernel (see ED_KERNELS).

    Serpentine (boustrophedon) scanning alternates row direction, which breaks
    up the directional "worm" artifacts that a one-way raster produces.
    """
    div, taps = ED_KERNELS[kernel]
    img = gray.astype(np.float32)
    h, w = img.shape
    for y in range(h):
        if y % 2 == 0:
            xs, flip = range(w), 1
        else:
            xs, flip = range(w - 1, -1, -1), -1
        for x in xs:
            old = img[y, x]
            new = 255.0 if old >= 128 else 0.0
            err = (old - new) / div
            img[y, x] = new
            for dy, dx, wt in taps:
                ny, nx = y + dy, x + dx * flip
                if 0 <= ny < h and 0 <= nx < w:
                    img[ny, nx] += err * wt
    return (img >= 128).astype(np.uint8) * 255


def dither_atkinson(gray: np.ndarray) -> np.ndarray:
    return error_diffuse(gray, "atkinson")


def dither_ordered(gray: np.ndarray, n: int = 8) -> np.ndarray:
    """Bayer ordered dithering (dispersed-dot)."""
    base = np.array([[0, 2], [3, 1]], dtype=np.float32)
    m = base
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    m = m[:n, :n]
    thr = (m + 0.5) / (m.size) * 255.0
    th = m.shape[0]
    tiled = np.tile(thr, (gray.shape[0] // th + 1, gray.shape[1] // th + 1))
    tiled = tiled[:gray.shape[0], :gray.shape[1]]
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


def dither_clustered(gray: np.ndarray, cell: int = 6) -> np.ndarray:
    """Clustered-dot (AM) screening. Dots grow in a cluster, producing long runs
    that compress far better and survive a noisy line. `cell` is scaled from the
    fax dpi by the caller so the screen doesn't collapse after re-thresholding."""
    # spiral-ordered threshold within a cell -> growth from center outward
    yy, xx = np.mgrid[0:cell, 0:cell]
    cx = cy = (cell - 1) / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    order = dist.argsort(axis=None).argsort().reshape(cell, cell)
    thr = (order + 0.5) / (cell * cell) * 255.0
    tiled = np.tile(thr, (gray.shape[0] // cell + 1, gray.shape[1] // cell + 1))
    tiled = tiled[:gray.shape[0], :gray.shape[1]]
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


# --- Blue-noise (void-and-cluster) FM screening -------------------------------
# Ulichney's void-and-cluster method builds an isotropic ("blue noise") threshold
# matrix: no directional structure, no clustered low-frequency blotches. We
# generate one 64x64 tile once and cache it as an asset (numpy-only, no scipy).
_BLUE_NOISE = None


def _gaussian_toroidal(arr: np.ndarray, sigma: float) -> np.ndarray:
    """Wrap-around (tileable) Gaussian filter via FFT, matching scipy's
    fourier_gaussian semantics so void/cluster ranking is stable."""
    fy = np.fft.fftfreq(arr.shape[0])[:, None]
    fx = np.fft.fftfreq(arr.shape[1])[None, :]
    g = np.exp(-2.0 * (np.pi ** 2) * (sigma ** 2) * (fx ** 2 + fy ** 2))
    return np.fft.ifft2(np.fft.fft2(arr) * g).real


def _largest_void(binary: np.ndarray, sigma: float) -> int:
    bp = binary if (np.count_nonzero(binary) * 2 < binary.size) else ~binary
    f = _gaussian_toroidal(np.where(bp, 1.0, 0.0), sigma)
    return int(np.argmin(np.where(bp, 2.0, f)))


def _tightest_cluster(binary: np.ndarray, sigma: float) -> int:
    bp = binary if (np.count_nonzero(binary) * 2 < binary.size) else ~binary
    f = _gaussian_toroidal(np.where(bp, 1.0, 0.0), sigma)
    return int(np.argmax(np.where(bp, f, -1.0)))


def generate_blue_noise(shape=(64, 64), sigma=1.9, seed=0, frac=0.1) -> np.ndarray:
    """Void-and-cluster blue-noise dither array (Ulichney 1993). Returns an int
    array containing each rank 0..N-1 exactly once."""
    rng = np.random.default_rng(seed)
    n = int(np.prod(shape))
    n_one = max(1, min(int((n - 1) / 2), int(n * frac)))
    binary = np.zeros(shape, dtype=bool)
    binary.flat[rng.permutation(n)[:n_one]] = True
    while True:
        c = _tightest_cluster(binary, sigma)
        binary.flat[c] = False
        v = _largest_void(binary, sigma)
        if v == c:
            binary.flat[c] = True
            break
        binary.flat[v] = True
    dither = np.zeros(shape, dtype=np.int64)
    bp = binary.copy()
    for rank in range(n_one - 1, -1, -1):          # phase 1
        c = _tightest_cluster(bp, sigma)
        bp.flat[c] = False
        dither.flat[c] = rank
    bp = binary.copy()
    for rank in range(n_one, (n + 1) // 2):        # phase 2
        v = _largest_void(bp, sigma)
        bp.flat[v] = True
        dither.flat[v] = rank
    for rank in range((n + 1) // 2, n):            # phase 3
        c = _tightest_cluster(bp, sigma)
        bp.flat[c] = True
        dither.flat[c] = rank
    return dither


def _blue_noise_matrix() -> np.ndarray:
    global _BLUE_NOISE
    if _BLUE_NOISE is not None:
        return _BLUE_NOISE
    asset = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "assets", "bluenoise_64.npy")
    try:
        _BLUE_NOISE = np.load(asset)
    except Exception:
        _BLUE_NOISE = generate_blue_noise((64, 64), sigma=1.9, seed=0)
        try:
            os.makedirs(os.path.dirname(asset), exist_ok=True)
            np.save(asset, _BLUE_NOISE)
        except Exception:
            pass
    return _BLUE_NOISE


def dither_blue_noise(gray: np.ndarray) -> np.ndarray:
    m = _blue_noise_matrix()
    thr = (m.astype(np.float32) + 0.5) / m.size * 255.0
    th, tw = m.shape
    tiled = np.tile(thr, (gray.shape[0] // th + 1, gray.shape[1] // tw + 1))
    tiled = tiled[:gray.shape[0], :gray.shape[1]]
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


# The curated "top 5" halftone technologies offered in the comparison preview,
# spanning the design space: AM screening, FM/blue-noise, two error-diffusion
# variants, and ordered. (jarvis/stucki/sierra are also selectable via --dither.)
TOP5_METHODS = ["clustered", "blue-noise", "atkinson", "floyd", "ordered"]

HALFTONE_INFO = {
    "clustered": "Clustered-dot AM screening — longest runs, best G4 compression, "
                 "most robust over a noisy line; lowest apparent resolution.",
    "blue-noise": "Void-and-cluster blue noise (FM) — isotropic organic stipple, "
                  "great perceived detail, no directional worms; mid compression.",
    "atkinson": "Atkinson error diffusion — clean whites, crisp thin features; "
                "good detail, looser compression than screening.",
    "floyd": "Floyd-Steinberg error diffusion — classic, maximum detail; "
             "directional speckle is the worst case for G4 size and line noise.",
    "ordered": "Bayer ordered dithering — fast, predictable crosshatch; "
               "middling on both detail and compression.",
    "jarvis": "Jarvis-Judice-Ninke error diffusion — wide 12-tap kernel, very "
              "smooth tone; heavy speckle, large G4 size.",
    "stucki": "Stucki error diffusion — 12-tap, sharp and smooth for print; "
              "heavy speckle, large G4 size.",
    "sierra": "Sierra error diffusion — Jarvis-like smoothness, a little cheaper.",
    "none": "Hard Otsu threshold — no halftone; correct for pure text, line art, "
            "and barcodes/QR codes.",
}

# Accepted --dither aliases mapped to canonical names.
DITHER_ALIASES = {"threshold": "none", "bayer": "ordered", "blue": "blue-noise"}


def recommend_dither(photo_fraction: float, fax_heavy: bool) -> tuple:
    """Suggest the OPTIMAL halftone for this page; returns (method, reason)."""
    if photo_fraction < 0.03:
        return "none", ("Page is essentially text / line art (photo area "
                        f"{photo_fraction * 100:.0f}%); a hard threshold is the "
                        "sharpest and smallest — halftoning would only add noise.")
    if fax_heavy or photo_fraction > 0.45:
        return "clustered", (f"Large photo area ({photo_fraction * 100:.0f}%) or "
                             "fax-heavy mode: clustered-dot keeps runs long, so it "
                             "compresses best and survives a noisy line.")
    return "atkinson", (f"Moderate photo area ({photo_fraction * 100:.0f}%) on a "
                        "clean line: Atkinson keeps detail and clean whites without "
                        "the heavy speckle (and G4 bloat) of Floyd/Jarvis. For a "
                        "softer, more isotropic look, blue-noise is the close runner-up.")


def choose_dither(name: str, fax_heavy: bool, photo_fraction: float) -> str:
    if name and name != "auto":
        return DITHER_ALIASES.get(name, name)
    return recommend_dither(photo_fraction, fax_heavy)[0]


def halftone(gray: np.ndarray, name: str, vdpi: int) -> np.ndarray:
    name = DITHER_ALIASES.get(name, name)
    if name == "none":
        return threshold_otsu(gray)
    if name == "floyd":
        return dither_floyd(gray)
    if name == "ordered":
        return dither_ordered(gray)
    if name == "blue-noise":
        return dither_blue_noise(gray)
    if name == "clustered":
        cell = max(4, min(10, round(vdpi / 32)))  # scale screen to dpi
        return dither_clustered(gray, cell=cell)
    if name in ED_KERNELS:
        return error_diffuse(gray, name)
    raise ValueError(f"unknown dither: {name}")


# --------------------------------------------------------------------------- #
# Page assembly                                                               #
# --------------------------------------------------------------------------- #
def is_already_bilevel(page: fitz.Page) -> bool:
    """True if the page is a single full-page image that is already 1-bit."""
    try:
        imgs = page.get_images(full=True)
    except Exception:
        return False
    if len(imgs) != 1:
        return False
    xref = imgs[0][0]
    try:
        info = page.parent.extract_image(xref)
    except Exception:
        return False
    return info.get("bpc") == 1


def detect_washout_colors(page: fitz.Page) -> list:
    """Flag wash-out-prone colors present as text fills (yellow/light-blue/etc.)."""
    warns = set()
    try:
        d = page.get_text("dict")
    except Exception:
        return []
    for blk in d.get("blocks", []):
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                c = span.get("color", 0)
                r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                if lum > 180 and (r > 180 and g > 180 and b < 120):
                    warns.add("wash_out_color:yellow")
                elif lum > 190:
                    warns.add("wash_out_color:light")
    return sorted(warns)


def _prepare_page(page: fitz.Page, opt: FaxOptions):
    """Rasterize + pre-clean a page and compute its photo mask, once.

    Returns (gray, mask, photo_fraction, warnings, already_bilevel). The result
    is reused both by the real conversion and by the multi-method comparison so
    every halftone option is rendered from an identical starting point.
    """
    hdpi, vdpi = RESOLUTIONS[opt.resolution]
    warnings: list = []
    gray = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)

    if is_already_bilevel(page):
        return gray, np.zeros_like(gray, dtype=bool), 0.0, warnings, True

    if opt.deskew:
        gray, ang = deskew_gray(gray)
        if ang:
            warnings.append(f"deskew:{ang:.1f}deg")
    if opt.flatten_bg:
        gray = flatten_background(gray)

    if opt.segmentation == "none":
        mask = np.zeros_like(gray, dtype=bool)
    elif opt.segmentation == "variance":
        mask = variance_photo_mask(gray)
    else:  # embedded
        mask = photo_region_mask(page, gray.shape, hdpi, vdpi,
                                 opt.max_scanline_px, "embedded")
        if not mask.any():
            mask = variance_photo_mask(gray)

    photo_fraction = float(mask.mean()) if mask.size else 0.0
    return gray, mask, photo_fraction, warnings, False


def _apply_dither(gray, mask, dither_name, opt, vdpi) -> np.ndarray:
    """Hard-threshold the whole page, then overlay the chosen halftone inside the
    photo mask only (bounded to its bbox — the halftone is the expensive step)."""
    text_bw = threshold_otsu(gray)
    dither_name = DITHER_ALIASES.get(dither_name, dither_name)
    if not mask.any() or dither_name == "none":
        return text_bw
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub_ht = halftone(gray[y0:y1, x0:x1], dither_name, vdpi)
    bw = text_bw.copy()
    sub_mask = mask[y0:y1, x0:x1]
    region = bw[y0:y1, x0:x1]
    region[sub_mask] = sub_ht[sub_mask]
    bw[y0:y1, x0:x1] = region
    return bw


def process_page(page: fitz.Page, idx: int, opt: FaxOptions) -> tuple[Image.Image, PageReport]:
    rep = PageReport(index=idx)
    _hdpi, vdpi = RESOLUTIONS[opt.resolution]
    gray, mask, photo_fraction, warnings, already = _prepare_page(page, opt)
    rep.warnings.extend(warnings)
    rep.photo_fraction = round(photo_fraction, 4)

    if already:
        rep.already_bilevel = True
        rep.dither = "none"
        return _finalize(threshold_otsu(gray), rep, opt), rep

    rep.photo_regions = int(mask.any())
    dname = choose_dither(opt.dither, opt.fax_heavy, photo_fraction)
    rep.dither = dname
    bw = _apply_dither(gray, mask, dname, opt, vdpi)

    if (bw == 0).mean() > 0.45:
        rep.warnings.append("inverted_or_heavy_black")
    rep.warnings.extend(detect_washout_colors(page))
    return _finalize(bw.astype(np.uint8), rep, opt), rep


def _postclean(bw: np.ndarray, opt: FaxOptions) -> np.ndarray:
    if opt.despeckle:
        bw = despeckle_bw(bw)
    if opt.thicken:
        bw = thicken_bw(bw)
    return bw


def _finalize(bw: np.ndarray, rep: PageReport, opt: FaxOptions) -> Image.Image:
    # Pillow 1-bit: 0=black,255=white -> mode '1'
    return Image.fromarray(_postclean(bw, opt)).convert("1")


# --------------------------------------------------------------------------- #
# Encoding / packing                                                          #
# --------------------------------------------------------------------------- #
def encode_g4_tiff(img: Image.Image, path: str) -> int:
    img.save(path, format="TIFF", compression="group4")
    return os.path.getsize(path)


def convert_pdf(in_pdf: str, out_path: str, opt: FaxOptions) -> dict:
    """Convert every page and write a G4 PDF or a Class-F multipage TIFF.
    Returns the report dict."""
    doc = fitz.open(in_pdf)
    tmpdir = tempfile.mkdtemp(prefix="faxopt_")
    tiff_paths, pages = [], []

    for i, page in enumerate(doc, start=1):
        img, rep = process_page(page, i, opt)
        tp = os.path.join(tmpdir, f"p{i:04d}.tif")
        nbytes = encode_g4_tiff(img, tp)
        rep.encoded_bytes = nbytes
        rep.est_transmission_s = round(
            nbytes * 8 / opt.line_rate_bps + opt.page_overhead_s, 1)
        tiff_paths.append(tp)
        pages.append(rep)

    if opt.fmt == "tiff":
        _save_multipage_tiff(tiff_paths, out_path)
    else:
        # img2pdf embeds CCITT G4 losslessly (PDF carries CCITTFaxDecode)
        with open(out_path, "wb") as f:
            f.write(img2pdf.convert(tiff_paths))

    report = {
        "mode": "fax",
        "input": in_pdf,
        "output": out_path,
        "input_bytes": os.path.getsize(in_pdf),
        "output_bytes": os.path.getsize(out_path),
        "pages": [vars(p) for p in pages],
        "total_est_transmission_s": round(
            sum(p.est_transmission_s for p in pages), 1),
        "warnings": sorted({w for p in pages for w in p.warnings}),
    }
    return report


def _save_multipage_tiff(tiff_paths, out_path):
    frames = [Image.open(p) for p in tiff_paths]
    first, rest = frames[0], frames[1:]
    first.save(out_path, format="TIFF", compression="group4",
               save_all=True, append_images=rest)


def render_preview(in_pdf: str, page_no: int, out_png: str, opt: FaxOptions):
    """Render exactly the bilevel output for one page as a PNG, for inspection."""
    doc = fitz.open(in_pdf)
    page = doc[page_no - 1]
    img, _ = process_page(page, page_no, opt)
    img.convert("L").save(out_png)
    return out_png


# --------------------------------------------------------------------------- #
# Multi-method comparison preview ("spend your eye tokens here")              #
# --------------------------------------------------------------------------- #
def _load_font(size: int):
    from PIL import ImageFont
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                 "Helvetica.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _compose_contact_sheet(panels, metrics, recommended, cell_w=480):
    """panels: list of (method_name, PIL '1' image). Returns an RGB contact
    sheet with a labeled, metric-annotated panel per method."""
    from PIL import ImageDraw
    rendered = []
    for name, img in panels:
        rgb = img.convert("L").convert("RGB")
        ch = max(1, int(rgb.height * (cell_w / rgb.width)))
        rendered.append((name, rgb.resize((cell_w, ch), Image.LANCZOS)))

    cap, pad, title_h = 56, 16, 70
    cols = 3 if len(rendered) > 4 else max(1, len(rendered))
    rows = (len(rendered) + cols - 1) // cols
    cell_h = max(r.height for _, r in rendered)
    W = pad + cols * (cell_w + pad)
    H = title_h + rows * (cell_h + cap + pad) + pad
    canvas = Image.new("RGB", (W, H), (245, 245, 245))
    d = ImageDraw.Draw(canvas)
    tf, cf, sf = _load_font(28), _load_font(19), _load_font(15)
    d.text((pad, 16), "Fax halftone comparison \u2014 spend your eye tokens: "
           "pick the panel that reads best", font=tf, fill=(15, 15, 15))

    for i, (name, rgb) in enumerate(rendered):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = title_h + r * (cell_h + cap + pad)
        is_rec = (name == recommended)
        d.rectangle([x, y, x + cell_w, y + cap],
                    fill=(206, 234, 206) if is_rec else (224, 224, 224))
        d.text((x + 8, y + 6),
                name.upper() + ("   >> RECOMMENDED <<" if is_rec else ""),
                font=cf, fill=(10, 10, 10))
        m = metrics.get(name, {})
        d.text((x + 8, y + 30),
                f"{m.get('encoded_bytes', 0) / 1024:.0f} KB  \u00b7  "
                f"~{m.get('est_transmission_s', 0):.0f}s / page",
                font=sf, fill=(70, 70, 70))
        canvas.paste(rgb, (x, y + cap))
        d.rectangle([x, y, x + cell_w, y + cap + rgb.height],
                    outline=(38, 148, 38) if is_rec else (178, 178, 178),
                    width=3 if is_rec else 1)
    return canvas


def render_comparison(in_pdf: str, page_no: int, out_png: str, opt: FaxOptions,
                      methods=None) -> dict:
    """Render one page through several halftone methods into a single contact
    sheet, annotated with per-method G4 size + transmission estimate and a
    recommended pick, so a human can choose the optimal one by eye ("eye
    tokens"). Returns a dict of metrics + recommendation."""
    methods = methods or TOP5_METHODS
    _hdpi, vdpi = RESOLUTIONS[opt.resolution]
    doc = fitz.open(in_pdf)
    page = doc[page_no - 1]
    gray, mask, photo_fraction, _warn, already = _prepare_page(page, opt)

    tmpdir = tempfile.mkdtemp(prefix="faxcmp_")
    panels, metrics = [], {}
    for m in methods:
        bw = _postclean(_apply_dither(gray, mask, m, opt, vdpi), opt)
        img = Image.fromarray(bw.astype(np.uint8)).convert("1")
        tp = os.path.join(tmpdir, f"{m}.tif")
        nbytes = encode_g4_tiff(img, tp)
        metrics[m] = {
            "encoded_bytes": nbytes,
            "est_transmission_s": round(
                nbytes * 8 / opt.line_rate_bps + opt.page_overhead_s, 1),
            "info": HALFTONE_INFO.get(DITHER_ALIASES.get(m, m), ""),
        }
        panels.append((m, img))

    rec, reason = recommend_dither(photo_fraction, opt.fax_heavy)
    if rec not in metrics:  # ensure the recommended panel is shown/highlighted
        rec = min(metrics, key=lambda k: metrics[k]["encoded_bytes"])
    _compose_contact_sheet(panels, metrics, rec).save(out_png)
    smallest = min(metrics, key=lambda k: metrics[k]["encoded_bytes"])
    return {
        "page": page_no,
        "already_bilevel": already,
        "photo_fraction": round(photo_fraction, 4),
        "methods": metrics,
        "recommended": rec,
        "reason": reason,
        "smallest": smallest,
        "output": out_png,
    }
