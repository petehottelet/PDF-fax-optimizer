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
    return np.asarray(Image.fromarray(gray, "L").convert("1")).astype(np.uint8) * 255


def dither_atkinson(gray: np.ndarray) -> np.ndarray:
    """Atkinson error diffusion: diffuses 6/8 of the error, cleaner whites and
    better thin-feature survival than Floyd-Steinberg, slightly better runs."""
    img = gray.astype(np.float32)
    h, w = img.shape
    for y in range(h):
        for x in range(w):
            old = img[y, x]
            new = 255.0 if old >= 128 else 0.0
            err = (old - new) / 8.0
            img[y, x] = new
            for dy, dx in ((0, 1), (0, 2), (1, -1), (1, 0), (1, 1), (2, 0)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w:
                    img[ny, nx] += err
    return (img >= 128).astype(np.uint8) * 255


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


def choose_dither(name: str, fax_heavy: bool, photo_fraction: float) -> str:
    if name != "auto":
        return name
    if fax_heavy or photo_fraction > 0.45:
        return "clustered"
    return "atkinson"


def halftone(gray: np.ndarray, name: str, vdpi: int) -> np.ndarray:
    if name == "none":
        return threshold_otsu(gray)
    if name == "floyd":
        return dither_floyd(gray)
    if name == "atkinson":
        return dither_atkinson(gray)
    if name == "ordered":
        return dither_ordered(gray)
    if name == "clustered":
        cell = max(4, min(10, round(vdpi / 32)))  # scale screen to dpi
        return dither_clustered(gray, cell=cell)
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


def process_page(page: fitz.Page, idx: int, opt: FaxOptions) -> tuple[Image.Image, PageReport]:
    rep = PageReport(index=idx)
    hdpi, vdpi = RESOLUTIONS[opt.resolution]

    if is_already_bilevel(page):
        rep.already_bilevel = True
        gray = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)
        bw = threshold_otsu(gray)
        return _finalize(bw, rep, opt), rep

    gray = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)

    if opt.deskew:
        gray, ang = deskew_gray(gray)
        if ang:
            rep.warnings.append(f"deskew:{ang:.1f}deg")
    if opt.flatten_bg:
        gray = flatten_background(gray)

    # --- MRC segmentation: photo mask ---
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
    rep.photo_regions = int(mask.any())

    # --- route: hard threshold everywhere, then overlay halftone in photo mask.
    # The halftone (esp. error diffusion) is the expensive step, so compute it
    # only within the photo region's bounding box rather than the whole page.
    text_bw = threshold_otsu(gray)
    if mask.any():
        dname = choose_dither(opt.dither, opt.fax_heavy, photo_fraction)
        ys, xs = np.where(mask)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        sub_ht = halftone(gray[y0:y1, x0:x1], dname, vdpi)
        bw = text_bw.copy()
        sub_mask = mask[y0:y1, x0:x1]
        region = bw[y0:y1, x0:x1]
        region[sub_mask] = sub_ht[sub_mask]
        bw[y0:y1, x0:x1] = region
    else:
        bw = text_bw

    # inverted-region warning: large black fraction transmits slowly
    if (bw == 0).mean() > 0.45:
        rep.warnings.append("inverted_or_heavy_black")

    rep.warnings.extend(detect_washout_colors(page))
    return _finalize(bw.astype(np.uint8), rep, opt), rep


def _finalize(bw: np.ndarray, rep: PageReport, opt: FaxOptions) -> Image.Image:
    if opt.despeckle:
        bw = despeckle_bw(bw)
    if opt.thicken:
        bw = thicken_bw(bw)
    # Pillow 1-bit: 0=black,255=white -> mode '1'
    return Image.fromarray(bw).convert("1")


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
