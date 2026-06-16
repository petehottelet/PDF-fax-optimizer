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
    dither: str = "auto"            # photo halftone schema (see HALFTONE_INFO)
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
    # Document-optimization knobs (halftoning-schemas.md)
    text_binarize: str = "contrast"  # contrast|sauvola|niblack|wolf|bradley|otsu
    tone_curve: str = "auto"        # auto|none  (dot-gain pre-correction)
    sharpen: bool = False           # edge-aware unsharp on photo regions
    green_noise_coarseness: float = 4.0  # green-noise AM<->FM knob (~2..8)
    text_in_image: bool = True      # rescue text baked into photos (don't halftone it)


@dataclass
class PageReport:
    index: int
    encoded_bytes: int = 0
    est_transmission_s: float = 0.0
    photo_regions: int = 0
    photo_fraction: float = 0.0
    dither: str = ""
    text_binarize: str = ""
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


def render_page_color(page: fitz.Page, hdpi: int, vdpi: int,
                      max_w: int) -> np.ndarray:
    """Render a page to an RGB ndarray at the same grid as render_page_gray.

    Used only for the comparison contact sheet's reference panel, so a viewer can
    see the original color document next to its grayscale and halftoned versions.
    """
    sx, sy = hdpi / 72.0, vdpi / 72.0
    page_w_pt = page.rect.width
    if page_w_pt * sx > max_w:
        sx = max_w / page_w_pt
    mat = fitz.Matrix(sx, sy)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    return arr[:, :, :3].copy()


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
                        var_lo: float = 80.0, var_hi: float = 4000.0,
                        min_area_frac: float = 0.015) -> np.ndarray:
    """Heuristic photo mask for flattened scans.

    Text/line-art blocks are bimodal (very high variance, sparse); flat
    background is near-zero variance; continuous-tone photo blocks fall in a
    mid band. We mark mid-variance blocks and clean up with morphology.

    Text protection: gray / anti-aliased text often lands in the mid band, and a
    naive CLOSE then welds those strokes into the photo blob — so the text gets
    halftoned and becomes hard to read (exactly what we must avoid). We therefore
    OPEN first (with a small kernel) to erase thin text strokes, CLOSE to
    consolidate the genuine photo interior, then keep only large connected
    components. Real photos are big contiguous regions; text-induced specks are
    small and get dropped, so halftoning stays on image sections only.
    """
    h, w = gray.shape
    gf = gray.astype(np.float32)
    mean = cv2.boxFilter(gf, -1, (block, block))
    sq = cv2.boxFilter(gf * gf, -1, (block, block))
    var = np.clip(sq - mean * mean, 0, None)
    band = ((var > var_lo) & (var < var_hi)).astype(np.uint8)
    k_open = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (max(3, block // 3), max(3, block // 3)))
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (block, block))
    band = cv2.morphologyEx(band, cv2.MORPH_OPEN, k_open)
    band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, k_close)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(band, 8)
    out = np.zeros_like(band)
    min_area = min_area_frac * h * w
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            out[labels == i] = 1
    return out.astype(bool)


# --------------------------------------------------------------------------- #
# Pre-cleaning                                                                #
# --------------------------------------------------------------------------- #
def flatten_background(gray: np.ndarray, knee: int = 235) -> np.ndarray:
    """Push near-white pixels to pure white so the background is clean, WITHOUT
    brightening the midtones. The old version stretched everything below the knee
    toward white, which washed out gray / light-gray text (a legibility killer);
    the text binarizer below is responsible for pulling that text to solid black,
    so here we only clamp the near-white paper and leave real content intact."""
    out = gray.copy()
    out[out >= knee] = 255
    return out


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


# --- Adaptive text binarization (document-imaging, not dithering) ------------ #
# These produce crisp 1-bit *text*, holding up under uneven illumination, dark
# header bars and reverse (white-on-black) type far better than a single global
# Otsu cut. They run before any halftone so text pixels never reach a screen.
def _local_mean_std(gray: np.ndarray, r: int):
    """Windowed mean and std via integral images: O(1) per pixel."""
    g = gray.astype(np.float64)
    k = 2 * r + 1
    ii = cv2.integral(g)                 # (h+1, w+1)
    ii2 = cv2.integral(g * g)
    h, w = g.shape

    def _winsum(I):
        y0 = np.clip(np.arange(h) - r, 0, h)
        y1 = np.clip(np.arange(h) + r + 1, 0, h)
        x0 = np.clip(np.arange(w) - r, 0, w)
        x1 = np.clip(np.arange(w) + r + 1, 0, w)
        A = I[np.ix_(y0, x0)]; B = I[np.ix_(y0, x1)]
        C = I[np.ix_(y1, x0)]; D = I[np.ix_(y1, x1)]
        return D - B - C + A

    cnt = (np.clip(np.arange(h) + r + 1, 0, h) - np.clip(np.arange(h) - r, 0, h))[:, None] * \
          (np.clip(np.arange(w) + r + 1, 0, w) - np.clip(np.arange(w) - r, 0, w))[None, :]
    cnt = np.maximum(cnt, 1)
    mean = _winsum(ii) / cnt
    var = np.maximum(_winsum(ii2) / cnt - mean * mean, 0.0)
    return mean, np.sqrt(var)


def binarize_text(gray: np.ndarray, method: str, vdpi: int) -> np.ndarray:
    """Return a 0/255 bilevel image (0=black) for text/line content.

    The whole point of a fax is to be READ, so text is never halftoned — it is
    thresholded to maximize contrast. The default ('contrast') is tuned for
    legibility: it marks black wherever a pixel is darker than its local paper by
    a small margin, so gray / light-gray text on a white background is pulled to
    SOLID BLACK instead of being dropped (the failure mode of a conservative
    adaptive cut). Smooth, text-free areas stay clean white (no speckle)."""
    method = (method or "contrast").lower()
    if method == "otsu":
        return threshold_otsu(gray)
    r = max(8, int(round(vdpi / 12)))    # window ~ half a body-text x-height
    g = gray.astype(np.float64)
    mean, std = _local_mean_std(gray, r)
    if method == "contrast":
        # Additive local-paper threshold (high recall on light text). Near-white
        # paper has mean ~250, so anything more than ~5% below it goes black; in
        # truly flat areas pixel ~= mean so nothing flips -> no background noise.
        thr = mean - 13.0
    elif method == "bradley":
        thr = mean * (1.0 - 0.15)
    elif method == "niblack":
        thr = mean - 0.2 * std
    elif method == "wolf":
        R = std.max() if std.max() > 1e-6 else 1.0
        M = float(g.min())
        thr = mean - 0.5 * (1.0 - std / R) * (mean - M)
    else:  # sauvola
        R, k = 128.0, 0.34
        thr = mean * (1.0 + k * (std / R - 1.0))
    black = g <= thr
    black |= _solid_fill_mask(g, mean)
    return np.where(black, 0, 255).astype(np.uint8)


def text_in_image_mask(gray: np.ndarray, vdpi: int) -> np.ndarray:
    """Detect text *baked into a photo* (captions, signs, screenshots, labels, a
    whole page scanned as one image) so it can be kept legible instead of being
    halftoned into mush.

    Text strokes are thin, high-contrast and arranged in roughly horizontal runs.
    A top-hat / black-hat (morphological) response isolates thin light/dark
    structures while suppressing smooth photo gradients; we keep only strong
    responses, group them into horizontal lines, and accept components whose
    geometry looks like a text line (wide, short, reasonably dense). This is
    deliberately conservative — it would rather miss faint text than carve harsh
    black blobs out of ordinary photo detail."""
    g = gray
    sw = max(2, int(round(vdpi / 90)))            # ~ stroke-width scale (px)
    k_stroke = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * sw + 1, 2 * sw + 1))
    resp = np.maximum(cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, k_stroke),  # dark text
                      cv2.morphologyEx(g, cv2.MORPH_TOPHAT, k_stroke))    # light text
    strokes = (resp >= 45).astype(np.uint8)        # strong-contrast strokes only
    kw = max(6, int(round(vdpi / 14)))
    lines = cv2.morphologyEx(
        strokes, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 1)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(lines, 8)
    out = np.zeros_like(lines)
    min_w = max(int(vdpi / 6), 16)
    max_h = int(vdpi * 0.5)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w < min_w or h < 3 or h > max_h:
            continue
        if w / float(h) < 2.2:                     # text lines are wide, not blobby
            continue
        if area / float(w * h) < 0.30:
            continue
        out[labels == i] = 1
    if out.any():                                  # expand from edges to glyph bodies
        out = cv2.dilate(
            out, cv2.getStructuringElement(cv2.MORPH_RECT, (3 * sw, 2 * sw)))
    return out.astype(bool)


def _solid_fill_mask(g: np.ndarray, mean: np.ndarray,
                     dark_level: float = 115.0,
                     bright: float = 160.0) -> np.ndarray:
    """Carry solid color-fill areas across as solid black.

    Adaptive binarizers (Sauvola/Niblack/...) misfire on large dark fills — e.g.
    a reverse-type header bar: inside the fill the local contrast term drives the
    threshold below the fill level, so the fill flips to white and only the glyph
    edges survive as a stroke. Where the *local mean* is dark (a filled region —
    using the mean rather than the per-pixel value rides out rasterization noise
    that a flat-std test trips over), keep the area solid black; only genuinely
    bright pixels (reverse/knockout text, `g >= bright`) are left white. Body text
    on a white page is unaffected: its local mean stays light."""
    return (mean < dark_level) & (g < bright)


# --- Photo pre-conditioning (applied before the halftone, photo region only) - #
# Effective dot gain on a 1-bit channel is large: without correction midtones
# plug to solid black and a photo arrives as a silhouette. Screens gain more
# than error diffusion, so the correction is calibrated per family. gamma<1
# lifts midtones (counteracts the darkening).
_TONE_GAMMA = {
    "clustered": 0.62, "ordered": 0.66, "blue-noise": 0.70, "green-noise": 0.66,
    "floyd": 0.85, "atkinson": 0.82, "jarvis": 0.85, "stucki": 0.85,
    "sierra": 0.85, "edd": 0.82, "line": 0.90,
}


def apply_tone_curve(gray: np.ndarray, dither_name: str, mode: str) -> np.ndarray:
    """Per-family dot-gain pre-correction. mode: 'auto' | 'none'."""
    if mode == "none":
        return gray
    gamma = _TONE_GAMMA.get(dither_name)
    if not gamma:
        return gray
    lut = (((np.arange(256) / 255.0) ** gamma) * 255.0).astype(np.uint8)
    return lut[gray]


def pre_sharpen(gray: np.ndarray, vdpi: int, amount: float = 0.7) -> np.ndarray:
    """Edge-aware unsharp mask to counter the channel's MTF rolloff."""
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=1.0)
    out = gray.astype(np.float32) + amount * (gray.astype(np.float32)
                                              - blur.astype(np.float32))
    return np.clip(out, 0, 255).astype(np.uint8)


def _aniso_tile(tile: np.ndarray, hdpi: int, vdpi: int) -> np.ndarray:
    """Resample a screen/threshold tile to the device pixel aspect so a dot is
    round *on paper* (pixels are non-square: standard 2:1, fine 1:1, superfine
    ~1:2). Vertical scale = vdpi/hdpi."""
    scale = vdpi / float(hdpi)
    th = max(2, int(round(tile.shape[0] * scale)))
    if th == tile.shape[0]:
        return tile
    return cv2.resize(tile, (tile.shape[1], th), interpolation=cv2.INTER_NEAREST)


def _tile_to(thr: np.ndarray, shape) -> np.ndarray:
    th, tw = thr.shape
    tiled = np.tile(thr, (shape[0] // th + 1, shape[1] // tw + 1))
    return tiled[:shape[0], :shape[1]]


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


def dither_ordered(gray: np.ndarray, n: int = 8, hdpi: int = 204,
                   vdpi: int = 196) -> np.ndarray:
    """Bayer ordered dithering (dispersed-dot), anisotropy-corrected."""
    base = np.array([[0, 2], [3, 1]], dtype=np.float32)
    m = base
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    m = m[:n, :n]
    thr = (m + 0.5) / (m.size) * 255.0
    thr = _aniso_tile(thr, hdpi, vdpi)
    tiled = _tile_to(thr, gray.shape)
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


def dither_clustered(gray: np.ndarray, cell: int = 6, hdpi: int = 204,
                     vdpi: int = 196) -> np.ndarray:
    """Clustered-dot (AM) screening. Dots grow in a cluster, producing long runs
    that compress far better and survive a noisy line. `cell` is scaled from the
    fax dpi by the caller so the screen doesn't collapse after re-thresholding,
    and the tile is resampled to the device aspect so dots stay round on paper."""
    # spiral-ordered threshold within a cell -> growth from center outward
    yy, xx = np.mgrid[0:cell, 0:cell]
    cx = cy = (cell - 1) / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    order = dist.argsort(axis=None).argsort().reshape(cell, cell)
    thr = (order + 0.5) / (cell * cell) * 255.0
    thr = _aniso_tile(thr.astype(np.float32), hdpi, vdpi)
    tiled = _tile_to(thr, gray.shape)
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


# Cache of generated void-and-cluster matrices, keyed by sigma. Blue noise uses
# a small sigma (isotropic, max detail); larger sigma clumps the minority pixels
# into mid-size aggregates -> "green noise" (the AM<->FM hybrid of section 1.1).
_VC_CACHE = {}


def _vc_matrix(sigma: float, tag: str) -> np.ndarray:
    key = round(sigma, 2)
    if key in _VC_CACHE:
        return _VC_CACHE[key]
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "assets")
    asset = os.path.join(assets, f"{tag}_64_s{key}.npy")
    try:
        m = np.load(asset)
    except Exception:
        m = generate_blue_noise((64, 64), sigma=sigma, seed=0)
        try:
            os.makedirs(assets, exist_ok=True)
            np.save(asset, m)
        except Exception:
            pass
    _VC_CACHE[key] = m
    return m


def _blue_noise_matrix() -> np.ndarray:
    global _BLUE_NOISE
    if _BLUE_NOISE is not None:
        return _BLUE_NOISE
    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "assets", "bluenoise_64.npy")
    try:
        _BLUE_NOISE = np.load(legacy)
    except Exception:
        _BLUE_NOISE = _vc_matrix(1.9, "bluenoise")
    return _BLUE_NOISE


def _screen_from_matrix(gray, m, hdpi, vdpi) -> np.ndarray:
    thr = (m.astype(np.float32) + 0.5) / m.size * 255.0
    thr = _aniso_tile(thr, hdpi, vdpi)
    tiled = _tile_to(thr, gray.shape)
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


def dither_blue_noise(gray: np.ndarray, hdpi: int = 204,
                      vdpi: int = 196) -> np.ndarray:
    return _screen_from_matrix(gray, _blue_noise_matrix(), hdpi, vdpi)


def dither_green_noise(gray: np.ndarray, coarseness: float = 4.0,
                       hdpi: int = 204, vdpi: int = 196) -> np.ndarray:
    """Green-noise / hybrid AM-FM screening (section 1.1). Built by running
    void-and-cluster with a larger Gaussian sigma so minority pixels coalesce
    into mid-size clusters: a tunable point between blue-noise detail and
    clustered-dot run-length/robustness. `coarseness` ~2 (near blue) .. 8 (near
    clustered)."""
    sigma = max(2.0, min(8.0, coarseness))
    return _screen_from_matrix(gray, _vc_matrix(sigma, "greennoise"), hdpi, vdpi)


def dither_edd(gray: np.ndarray, lam: float = 0.4, vdpi: int = 196) -> np.ndarray:
    """Edge-enhancing error diffusion (Eschbach, section 2.3): add a high-pass
    term before diffusing so glyph/edge structure survives while tone still
    diffuses. Good for text sitting over a photographic background."""
    sharp = pre_sharpen(gray, vdpi, amount=lam * 2.0)
    return error_diffuse(sharp, "atkinson")


def dither_line(gray: np.ndarray, hdpi: int = 204, vdpi: int = 196,
                period: int = 0) -> np.ndarray:
    """Horizontal line-screen halftone (woodcut / engraving look).

    Tone is rendered as horizontal stripes whose *thickness* grows with
    darkness: a triangular threshold profile across one vertical period is high
    at the line center (so even highlights keep a hairline) and low at the
    edges (so only deep shadows fill the gap). Because the lines run *along the
    scanline*, the result is almost entirely long horizontal black/white runs —
    the single most G4-friendly way to carry a continuous-tone image over a fax
    line, while reading as a clean engraving rather than mud. The period is
    derived from the vertical DPI (so stripe pitch is constant on paper)."""
    if period <= 0:
        # ~18 lines/inch: a bold, unmistakable woodcut pitch that survives both
        # the fax channel and being downscaled into a preview.
        period = max(4, int(round(vdpi / 18.0)))
    # Triangular threshold profile across one period. A high peak (250) keeps a
    # hairline even in highlights; a low edge (12) lets only deep shadows fill
    # the gap to near-solid. Black where gray <= thr, so the dark stripe THICKENS
    # smoothly as the local tone darkens — a line-conversion / engraving screen.
    y = np.arange(period, dtype=np.float32)
    center = (period - 1) / 2.0
    d = np.abs(y - center) / max(center, 1e-6)      # 0 at center .. 1 at edge
    peak, edge = 250.0, 12.0
    thr = peak - (peak - edge) * d
    tiled = _tile_to(thr.reshape(period, 1), gray.shape)
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


# The curated halftone technologies offered in the comparison preview, spanning
# the design space: AM screening, AM-FM hybrid, FM/blue-noise, two error-diffusion
# variants, and a horizontal line screen (woodcut). 6-up so the user can pick by
# eye. (ordered/edd/jarvis/stucki/sierra are also selectable via --dither.)
COMPARE_METHODS = ["clustered", "green-noise", "blue-noise", "atkinson",
                   "floyd", "line"]
# When both reference panels (original color + true grayscale) are shown first,
# use four halftone options so the sheet stays a clean 6-up: an AM screen, the
# AM-FM hybrid, an FM/blue-noise stipple, and the woodcut line screen.
COMPARE4_METHODS = ["clustered", "green-noise", "blue-noise", "line"]
# Backwards-compatible alias.
COMPARE5_METHODS = COMPARE4_METHODS
# Backwards-compatible alias for older callers.
TOP5_METHODS = COMPARE_METHODS

HALFTONE_INFO = {
    "clustered": "Clustered-dot AM screening — longest runs, best G4 compression, "
                 "most robust over a noisy line; lowest apparent resolution.",
    "green-noise": "Green-noise hybrid AM-FM — mid-size dot clusters: blue-noise "
                   "detail with clustered-dot run-length/robustness. Tunable via "
                   "coarseness; a strong default for a photo that must survive a "
                   "bad line.",
    "blue-noise": "Void-and-cluster blue noise (FM) — isotropic organic stipple, "
                  "great perceived detail, no directional worms; mid compression.",
    "atkinson": "Atkinson error diffusion — clean whites, crisp thin features; "
                "good detail, looser compression than screening.",
    "floyd": "Floyd-Steinberg error diffusion — classic, maximum detail; "
             "directional speckle is the worst case for G4 size and line noise.",
    "ordered": "Bayer ordered dithering — fast, predictable crosshatch; "
               "middling on both detail and compression.",
    "line": "Horizontal line screen (woodcut/engraving) — tone as horizontal "
            "stripes thickened by darkness; runs along the scanline so G4 size "
            "is excellent and it reads cleanly, high-contrast, never muddy.",
    "edd": "Edge-enhancing error diffusion — high-pass term sharpens edges while "
           "diffusing tone; for text over a photographic background.",
    "jarvis": "Jarvis-Judice-Ninke error diffusion — wide 12-tap kernel, very "
              "smooth tone; heavy speckle, large G4 size.",
    "stucki": "Stucki error diffusion — 12-tap, sharp and smooth for print; "
              "heavy speckle, large G4 size.",
    "sierra": "Sierra error diffusion — Jarvis-like smoothness, a little cheaper.",
    "none": "Hard threshold — no halftone; correct for pure text, line art, "
            "and barcodes/QR codes.",
}

# Accepted --dither aliases mapped to canonical names.
DITHER_ALIASES = {"threshold": "none", "bayer": "ordered", "blue": "blue-noise",
                  "green": "green-noise", "woodcut": "line", "engraving": "line",
                  "lines": "line"}


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
    return "green-noise", (f"Moderate photo area ({photo_fraction * 100:.0f}%): "
                           "green-noise hybrid keeps blue-noise-level detail but "
                           "clusters dots into longer runs, so it compresses and "
                           "survives the line far better than Floyd/Atkinson. Drop "
                           "to atkinson for the crispest whites on a clean line.")


def choose_dither(name: str, fax_heavy: bool, photo_fraction: float) -> str:
    if name and name != "auto":
        return DITHER_ALIASES.get(name, name)
    return recommend_dither(photo_fraction, fax_heavy)[0]


def halftone(gray: np.ndarray, name: str, hdpi: int, vdpi: int,
             coarseness: float = 4.0) -> np.ndarray:
    name = DITHER_ALIASES.get(name, name)
    if name == "none":
        return threshold_otsu(gray)
    if name == "floyd":
        return dither_floyd(gray)
    if name == "ordered":
        return dither_ordered(gray, hdpi=hdpi, vdpi=vdpi)
    if name == "blue-noise":
        return dither_blue_noise(gray, hdpi=hdpi, vdpi=vdpi)
    if name == "green-noise":
        return dither_green_noise(gray, coarseness=coarseness,
                                  hdpi=hdpi, vdpi=vdpi)
    if name == "edd":
        return dither_edd(gray, vdpi=vdpi)
    if name == "line":
        return dither_line(gray, hdpi=hdpi, vdpi=vdpi)
    if name == "clustered":
        cell = max(4, min(10, round(vdpi / 32)))  # scale screen to dpi
        return dither_clustered(gray, cell=cell, hdpi=hdpi, vdpi=vdpi)
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


def _apply_dither(gray, mask, dither_name, opt, hdpi, vdpi) -> np.ndarray:
    """Binarize text/line content with the chosen adaptive binarizer, then
    overlay the chosen halftone inside the photo mask only (bounded to its bbox).
    Photo regions get dot-gain pre-correction and optional edge sharpening first,
    so the channel renders the document well rather than just 'fax-ifying' it."""
    text_bw = binarize_text(gray, opt.text_binarize, vdpi)
    dither_name = DITHER_ALIASES.get(dither_name, dither_name)
    if not mask.any() or dither_name == "none":
        return text_bw
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = gray[y0:y1, x0:x1]
    if opt.sharpen:
        sub = pre_sharpen(sub, vdpi)
    sub = apply_tone_curve(sub, dither_name, opt.tone_curve)
    sub_ht = halftone(sub, dither_name, hdpi, vdpi, opt.green_noise_coarseness)
    if opt.text_in_image:
        # Rescue text baked into the photo: where strokes form text lines, keep
        # the legible binarization instead of the halftone so it stays readable.
        tmask = text_in_image_mask(gray[y0:y1, x0:x1], vdpi)
        if tmask.any():
            sub_ht = np.where(tmask, text_bw[y0:y1, x0:x1], sub_ht)
    bw = text_bw.copy()
    sub_mask = mask[y0:y1, x0:x1]
    region = bw[y0:y1, x0:x1]
    region[sub_mask] = sub_ht[sub_mask]
    bw[y0:y1, x0:x1] = region
    return bw


def process_page(page: fitz.Page, idx: int, opt: FaxOptions) -> tuple[Image.Image, PageReport]:
    rep = PageReport(index=idx)
    hdpi, vdpi = RESOLUTIONS[opt.resolution]
    gray, mask, photo_fraction, warnings, already = _prepare_page(page, opt)
    rep.warnings.extend(warnings)
    rep.photo_fraction = round(photo_fraction, 4)
    rep.text_binarize = opt.text_binarize

    if already:
        rep.already_bilevel = True
        rep.dither = "none"
        return _finalize(threshold_otsu(gray), rep, opt), rep

    rep.photo_regions = int(mask.any())
    dname = choose_dither(opt.dither, opt.fax_heavy, photo_fraction)
    rep.dither = dname
    bw = _apply_dither(gray, mask, dname, opt, hdpi, vdpi)

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


def _oswald_font(size: int, weight: str = "Bold"):
    """Condensed Oswald display face (matches the project logo) for headings.

    Prefers the bundled Oswald variable font; falls back to a condensed/bold
    system face, then to the generic loader, so it still renders anywhere."""
    from PIL import ImageFont
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "assets", "Oswald.ttf")
    try:
        f = ImageFont.truetype(bundled, size)
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
        return f
    except Exception:
        pass
    for name in ("Oswald-Bold.ttf", "BebasNeue-Regular.ttf", "impact.ttf",
                 "arialbd.ttf", "ariblk.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return _load_font(size)


def _compose_contact_sheet(panels, metrics, recommended, cell_w=480):
    """panels: list of (method_name, PIL '1' image). Returns an RGB contact
    sheet with a labeled, metric-annotated panel per method."""
    from PIL import ImageDraw
    rendered = []
    for name, img in panels:
        rgb = img.convert("RGB")          # keep color references in color
        ch = max(1, int(rgb.height * (cell_w / rgb.width)))
        rendered.append((name, rgb.resize((cell_w, ch), Image.LANCZOS)))

    cap, pad, title_h = 58, 18, 18
    cols = 3 if len(rendered) > 4 else max(1, len(rendered))
    rows = (len(rendered) + cols - 1) // cols
    cell_h = max(r.height for _, r in rendered)
    W = pad + cols * (cell_w + pad)
    H = title_h + rows * (cell_h + cap + pad) + pad
    canvas = Image.new("RGB", (W, H), (244, 244, 244))
    d = ImageDraw.Draw(canvas)
    lf, sf = _oswald_font(22, "SemiBold"), _load_font(15)

    for i, (name, rgb) in enumerate(rendered):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = title_h + r * (cell_h + cap + pad)
        m = metrics.get(name, {})
        is_orig = bool(m.get("original"))
        is_rec = (name == recommended) and not is_orig
        if is_orig:
            header = (210, 222, 240)        # neutral blue for the source panel
        elif is_rec:
            header = (206, 234, 206)
        else:
            header = (228, 228, 228)
        d.rectangle([x, y, x + cell_w, y + cap], fill=header)
        label = (m.get("label", name.upper()) if is_orig
                 else name.upper() + ("   >> RECOMMENDED <<" if is_rec else ""))
        d.text((x + 10, y + 6), label, font=lf, fill=(10, 10, 10))
        if is_orig:
            d.text((x + 10, y + 34), m.get("note", "source \u00b7 not a fax"),
                   font=sf, fill=(70, 70, 70))
        else:
            d.text((x + 10, y + 34),
                   f"{m.get('encoded_bytes', 0) / 1024:.0f} KB  \u00b7  "
                   f"~{m.get('est_transmission_s', 0):.0f}s / page",
                   font=sf, fill=(70, 70, 70))
        canvas.paste(rgb, (x, y + cap))
        d.rectangle([x, y, x + cell_w, y + cap + rgb.height],
                    outline=(38, 148, 38) if is_rec else (178, 178, 178),
                    width=3 if is_rec else 1)
    return canvas


def render_comparison(in_pdf: str, page_no: int, out_png: str, opt: FaxOptions,
                      methods=None, include_original: bool = False) -> dict:
    """Render one page through several halftone methods into a single contact
    sheet, annotated with per-method G4 size + transmission estimate and a
    recommended pick, so a human can choose the optimal one by eye ("eye
    tokens"). Returns a dict of metrics + recommendation.

    With include_original, two reference panels are shown first — the original in
    color (#1) and a true grayscale of it (#2) — and the default method set drops
    to four halftones so the sheet stays a clean 6-up. The references are not
    themselves faxes; they show the continuous-tone input each halftone (and the
    1-bit channel) has to approximate."""
    if methods is None:
        methods = COMPARE4_METHODS if include_original else COMPARE_METHODS
    hdpi, vdpi = RESOLUTIONS[opt.resolution]
    doc = fitz.open(in_pdf)
    page = doc[page_no - 1]
    gray, mask, photo_fraction, _warn, already = _prepare_page(page, opt)

    tmpdir = tempfile.mkdtemp(prefix="faxcmp_")
    panels, metrics = [], {}
    if include_original:
        color = render_page_color(page, hdpi, vdpi, opt.max_scanline_px)
        panels.append(("original", Image.fromarray(color, "RGB")))
        metrics["original"] = {
            "encoded_bytes": 0, "est_transmission_s": 0.0, "original": True,
            "label": "ORIGINAL (color)", "note": "color source \u00b7 not a fax",
            "info": "The original document in color, for reference.",
        }
        gray_ref = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)
        panels.append(("grayscale", Image.fromarray(gray_ref).convert("L")))
        metrics["grayscale"] = {
            "encoded_bytes": 0, "est_transmission_s": 0.0, "original": True,
            "label": "TRUE GRAYSCALE", "note": "grayscale source \u00b7 not a fax",
            "info": "True grayscale (desaturated) source — the continuous-tone "
                    "input each halftone below approximates.",
        }
    for m in methods:
        bw = _postclean(_apply_dither(gray, mask, m, opt, hdpi, vdpi), opt)
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
    if rec not in methods:  # ensure the recommended panel is shown/highlighted
        rec = min(methods, key=lambda k: metrics[k]["encoded_bytes"])
    _compose_contact_sheet(panels, metrics, rec).save(out_png)
    smallest = min(methods, key=lambda k: metrics[k]["encoded_bytes"])
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
