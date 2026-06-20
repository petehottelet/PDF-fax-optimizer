#!/usr/bin/env python3
"""Fax conversion pipeline.

Converts PDF pages to fax-native 1-bit (bilevel) output and packs them into a
CCITT-G4 PDF (default) or a Class-F multipage TIFF. Implements the decisions
described in references/fax-optimization.md:

  - square rasterization at the source's native resolution, hard-capped at
    300 PPI (the 1-bit fax channel's legibility ceiling)
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

# Square (isotropic) detail presets — the halftone screen detail and the target for
# vector-page rasterization. We render at the SOURCE'S NATIVE resolution where
# possible, capped at `MAX_RENDER_DPI` (300 PPI — the legibility plateau for the
# 1-bit fax channel). Vector PDFs rasterise at the preset DPI also clipped to the
# ceiling; raster sources whose native DPI exceeds 300 PPI are bicubic-downsampled
# to it. SUPERFINE is the default — its nominal 391 PPI lands at 300 PPI in
# practice, finer than fax-superfine's 196×391 anisotropic grid on either axis,
# and that's the most the channel can resolve.
RESOLUTIONS = {
    "standard": (98, 98),
    "fine": (196, 196),
    "superfine": (391, 391),
}
DEFAULT_RESOLUTION = "superfine"

# A real Group-3 fax line caps each scanline at 1728 px. Because we now render at
# the SOURCE'S native (or the preset's) resolution, scanlines can be wider; pass
# --transmission-safe to clamp back to 1728 for actual G3 transmission. The
# pipeline-internal cap below is a different beast: it only stops a pathologically
# low-DPI wrapped PDF (e.g. a 2320×3000 image declaring itself "32×41 inches at 72
# DPI") from blowing up to a 200-megapixel buffer when SUPERFINE's 391-DPI floor
# is applied. Pages above the cap simply render at a slightly lower square DPI.
MAX_SCANLINE_PX = 24000
# Hard ceiling on render DPI. 300 PPI is the legibility plateau for fax: a fine
# fax preset is 196 lpi vertical, super-fine is 391 lpi vertical anisotropic, and
# we run a SQUARE 300 PPI grid which exceeds both effective resolutions on the
# scan-line axis while keeping page buffers small (8.5×11 → ~8 MP). Rendering
# higher just makes the halftone CPU spend longer per pixel for no extra fidelity
# the 1-bit channel can carry. Raster sources whose native DPI exceeds the cap
# are bicubic-downsampled, which is fine — the binarizer copes with mild AA, and
# the halftone screen at 300 PPI is finer than the source pixel grid anyway.
MAX_RENDER_DPI = 300

# The #808080 polarity rule: any text whose surrounding field is darker than this
# is recolored WHITE; any text whose field is lighter is recolored BLACK. The fax
# channel is luminance-only, so this is the bright-line rule for legibility.
POLARITY_THRESHOLD = 128


@dataclass
class FaxOptions:
    resolution: str = DEFAULT_RESOLUTION
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
    recover_text: str = "off"   # off|on|auto  OCR-driven recolor of text inside images (opt-in)
    # Heuristic rescue for dark text sitting on ANY small saturated-colour
    # fill that loses contrast in grayscale: slide highlight chips, status
    # badges on dashboards, colored table cells, tinted callout boxes,
    # colored form fields, filled banners. In grayscale the bright fill
    # collapses to a mid-tone the contrast binarizer treats as "dark field,
    # light text", flips polarity, and shreds the glyphs. With this on, the
    # colored field is lifted to white BEFORE binarization so the dark text
    # reads cleanly. Off-by-default would have left a default-mode user
    # with broken labels on slide decks and dashboards — the failure was
    # loud enough that it warrants the default-on heuristic (matches the
    # existing text_in_image policy).
    preserve_text: bool = True
    recover_text_stroke: float = 0.15  # retained for back-compat; unused in the OCR/#808080 path
    ocr_text: str = "auto"           # off|auto|on  OCR-driven polarity for ALL page text
    ocr_conf_min: float = 0.5        # minimum OCR confidence to recolor a word
    transmission_safe: bool = False  # clamp scanline to 1728 px for real G3 transmission
    basic: bool = False              # bare-minimum pipeline: gray + Otsu, no MRC/OCR/cleanup
    # Generalised AM screen ("screen" dither) — selectable spot function + angle.
    # `clustered` is the historical name; passing --dither screen makes the shape
    # explicit. dot_shape: round|square|diamond|ellipse. screen_angle in degrees,
    # also applied to the `line` screen for diagonal engraving.
    dot_shape: str = "round"
    screen_angle: float = 0.0
    # Crosshatch — comma-separated angle list, default two perpendicular sets.
    # 2 perpendicular sets keep transition density near `line`; more angles
    # multiply transitions and push it into expressive territory.
    hatch_angles: tuple = (0.0, 90.0)


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
    recover_text: dict = field(default_factory=dict)
    ocr_text: dict = field(default_factory=dict)
    photo_features: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Rasterization                                                               #
# --------------------------------------------------------------------------- #
def _native_dpi(page: fitz.Page) -> float:
    """Return the largest embedded image's effective DPI on the page (square),
    or 0 for vector-only pages.

    A raster source (PNG/JPG wrapped to PDF, scanned page) carries native pixels.
    Rendering at less than its native DPI throws away detail, and the user has
    asked us to ALWAYS keep that detail. Take the highest pixels-per-inch across
    all embedded images so a single high-res image's grid is preserved."""
    try:
        imgs = page.get_images(full=True)
    except Exception:
        return 0.0
    best = 0.0
    for img in imgs:
        xref = img[0]
        try:
            info = page.parent.extract_image(xref)
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        wpx, hpx = info.get("width", 0), info.get("height", 0)
        for r in rects:
            iw, ih = r.width / 72.0, r.height / 72.0
            if iw > 0 and wpx > 0:
                best = max(best, wpx / iw)
            if ih > 0 and hpx > 0:
                best = max(best, hpx / ih)
    return best


def _effective_dpi(page: fitz.Page, preset_dpi: int) -> float:
    """Square render DPI for the page, hard-capped at `MAX_RENDER_DPI` (300).

    Vector pages (no embedded raster): use the preset DPI — text and line art
    are real glyphs the renderer rasterises crisp at any DPI — clipped to the
    ceiling. SUPERFINE's nominal 391 PPI lands at 300 PPI in practice; the
    1-bit fax channel can't resolve the extra 91 PPI of line art anyway.

    Raster pages: use NATIVE DPI, also clipped to the ceiling. Upscaling a
    wrapped raster bicubic-interpolates everything in the source including the
    text — thin strokes soften, the binarizer loses them, and "ABC" becomes
    "A8C" — so we never render above native. (Halftone fineness at low native
    DPI is recovered by picking an FM screen — `blue-noise` / `atkinson` —
    whose dot pitch is the pixel itself, not a multi-pixel cell;
    `choose_dither` does this automatically.) A 600-DPI scan downsamples to
    300 PPI on the way in, which is what we want — fax legibility caps out
    well below scanner-native resolution.

    The DPI ceiling makes per-page buffers predictable: 8.5×11" → ~8 MP,
    10×5.6" slide → ~5 MP, regardless of source resolution. Aspect ratio is
    always preserved (DPI is square)."""
    nat = _native_dpi(page)
    if nat > 0:
        return min(float(nat), float(MAX_RENDER_DPI))
    return min(float(preset_dpi), float(MAX_RENDER_DPI))


def _page_effective_dpi(page: fitz.Page, opt: "FaxOptions") -> int:
    """The integer effective DPI for `page` under `opt`. Single source of truth
    for both rendering AND halftone parameters: the screen cell size, blue-noise
    tile, etc. all key off this value, so the halftone always matches the actual
    pixel grid we rendered onto."""
    preset = RESOLUTIONS[opt.resolution][0]
    return int(round(_effective_dpi(page, preset)))


def _page_scale(page_w_pt: float, hdpi: int, vdpi: int, max_w: int):
    """Square render scale (points→px). The pipeline now runs at SQUARE pixels —
    `hdpi` and `vdpi` are equal — so the rendered page keeps the source's aspect
    ratio. If the rendered width would exceed `max_w` (only enabled in
    transmission-safe mode), scale BOTH axes uniformly so the page stays
    proportioned."""
    s = max(hdpi, vdpi) / 72.0           # always square; tolerate legacy callers
    if max_w > 0 and page_w_pt * s > max_w:
        s = max_w / page_w_pt
    return s, s


def render_page_gray(page: fitz.Page, hdpi: int, vdpi: int,
                     max_w: int) -> np.ndarray:
    """Render a page to a grayscale ndarray at the effective (square) DPI.

    Effective DPI = max(preset, native source DPI), so a high-resolution image is
    preserved pixel-for-pixel and a vector page is rendered at the preset's square
    detail. Pixels are square, so the page keeps its source aspect ratio."""
    eff = _effective_dpi(page, max(hdpi, vdpi))
    sx, sy = _page_scale(page.rect.width, int(round(eff)), int(round(eff)), max_w)
    mat = fitz.Matrix(sx, sy)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return arr.copy()


def render_page_color(page: fitz.Page, hdpi: int, vdpi: int,
                      max_w: int) -> np.ndarray:
    """Render a page to an RGB ndarray on the same square grid as render_page_gray.
    Used by OCR, recover-text, and the multi-panel sample sheet."""
    eff = _effective_dpi(page, max(hdpi, vdpi))
    sx, sy = _page_scale(page.rect.width, int(round(eff)), int(round(eff)), max_w)
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
        sx, sy = _page_scale(page.rect.width, hdpi, vdpi, max_w)
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
                        min_area_frac: float = 0.015,
                        rgb: np.ndarray | None = None,
                        chroma_seed: float = 2.0) -> np.ndarray:
    """Heuristic photo mask for flattened scans (used when no embedded image
    rectangles are available, or for the full-page wrapped-raster fallback).

    The trick on a wrapped form-and-photo page is that the photo has BOTH
    colourful regions (sky, foliage, signage) and grayscale interiors (a
    white car body, a dark grille). Earlier rules that gated on chroma OR on
    `var > var_lo` per block excised those grayscale interiors and the
    halftone became a swiss-cheese pattern — the SUV body and grille were
    hard-thresholded into chunky black/white blobs while the surrounding
    foliage and sky were halftoned smoothly. Filling the holes with
    morphological close didn't work either, because a large interior gap
    (the SUV, ~6% of the photo) was bigger than any kernel safely small
    enough not to also bridge into the form table / message text sitting a
    few pixels above/below the photo.

    The fix is to define the photo by the BOUNDING BOX of its colour-bearing
    seed regions, not by the seed pixels themselves. Real photos have a
    rectangular footprint; once we know that footprint, everything inside
    halftones (including the grayscale SUV) and everything outside binarizes
    (form table, body text, address). Steps:

    1. Compute `chroma_box` — the mean chroma in each `block`-sized window.
    2. Threshold at `chroma_seed` to find the photo's colour-bearing blocks.
    3. Morph-close with a `block`-sized kernel so the sky and the building
       and the billboard cluster into one seed blob, and keep components
       above `min_area_frac` of the page so a small colour logo (a 60x60
       gold flourish) doesn't sprout a halftone region of its own.
    4. The photo region is the BBOX of each surviving seed component.

    For grayscale sources (or pages whose colour content sits below the
    `chroma.max() > 4.0` detect threshold), the chroma path self-disables
    and we fall back to the historical mid-variance rule (a block is photo
    if `var_lo < var < var_hi` — text is high-variance, paper is zero, the
    middle band is photo) so a B/W scan still segments its photo regions.

    `var_lo` is unused on the colour path but retained for API
    compatibility and for the grayscale fallback.
    """
    h, w = gray.shape
    gf = gray.astype(np.float32)
    mean = cv2.boxFilter(gf, -1, (block, block))
    sq = cv2.boxFilter(gf * gf, -1, (block, block))
    var = np.clip(sq - mean * mean, 0, None)

    use_chroma = False
    chroma_box = None
    if rgb is not None and rgb.ndim == 3:
        rgbf = rgb.astype(np.float32)
        r, g, b = rgbf[..., 0], rgbf[..., 1], rgbf[..., 2]
        mc = (r + g + b) / 3.0
        chroma = np.sqrt(((r - mc) ** 2 + (g - mc) ** 2 + (b - mc) ** 2) / 3.0)
        if chroma.max() > 4.0:
            chroma_box = cv2.boxFilter(chroma, -1, (block, block))
            use_chroma = True

    min_area = min_area_frac * h * w

    if use_chroma:
        seed = (chroma_box > chroma_seed).astype(np.uint8)
        k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (block, block))
        seed = cv2.morphologyEx(seed, cv2.MORPH_CLOSE, k_close)
        n, _labels, stats, _cent = cv2.connectedComponentsWithStats(seed, 8)
        out = np.zeros((h, w), dtype=np.uint8)
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < min_area:
                continue
            x0 = stats[i, cv2.CC_STAT_LEFT]
            y0 = stats[i, cv2.CC_STAT_TOP]
            ww = stats[i, cv2.CC_STAT_WIDTH]
            hh = stats[i, cv2.CC_STAT_HEIGHT]
            out[y0:y0 + hh, x0:x0 + ww] = 1
        return out.astype(bool)

    # Grayscale fallback: mid-variance band + morphology + min-area filter.
    band = ((var > var_lo) & (var < var_hi)).astype(np.uint8)
    k_open = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (max(3, block // 3), max(3, block // 3)))
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (block, block))
    band = cv2.morphologyEx(band, cv2.MORPH_OPEN, k_open)
    band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, k_close)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(band, 8)
    out = np.zeros_like(band)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            out[labels == i] = 1
    return out.astype(bool)


def preserve_text_mask(rgb: np.ndarray,
                                gray: np.ndarray,
                                block: int = 24,
                                chroma_lo: float = 12.0,
                                max_area_frac: float = 0.015,
                                dark_thr: int = 110,
                                min_dark_frac: float = 0.0,
                                max_dark_frac: float = 0.50,
                                pad: int = 4,
                                bg_luma_floor: int = 60,
                                rel_dark_offset: int = 40,
                                rel_dark_floor: int = 40,
                                solid_chroma_density: float = 0.55) -> np.ndarray:
    """Find small saturated-colour fields that contain dark text strokes;
    return a mask of pixels to LIFT TO WHITE before binarization.

    The motivating failure mode: a page places dark text on top of ANY
    saturated-colour fill — a highlight chip behind a slide label, a
    status badge in a dashboard, a colored cell in a table, a tinted
    callout box, a colored form field, a filled banner. The original
    page is high-contrast and legible. After RGB→gray demotion the bright
    fill collapses to a mid-tone (~140 luma) and dark text on a dark-ish
    field has too little contrast to survive: the `contrast` text
    binarizer sees the colored area as a "dark field with light text",
    flips polarity, paints it solid black, and knocks the glyphs out —
    they arrive as a mangled crosshatch that's effectively illegible on
    the receiving end.

    The cure: in the gray image, drop the field's tone to white. The
    black text now reads as crisp black-on-white and binarizes cleanly.
    We sacrifice the "this is colored" visual cue, but on a 1-bit fax
    channel that cue was going to die anyway — the trade is legible
    text for a colour we couldn't render. Same playbook as the
    photo-region exclude: prefer text legibility over decorative tone.

    Detection (must satisfy *all*):
      - **High chroma** in the source RGB (`chroma_lo`) — saturated colour,
        not a barely-tinted gray. Subtle tints stay on the binarizer path.
      - **Small area** (`max_area_frac` of page; ~1.5%) — large coloured
        regions are photos, illustrations, full-bleed colour panels
        (e.g. a slide's lime backdrop), or signage on a billboard. Those
        have to keep their tone via the halftone path, not get blanked.
        The bound has to be tighter than a casual reader expects: a
        billboard panel inside a photo can clear the chroma seed AND the
        text-density gate but is far too big to whiten without erasing
        the photo. 1.5% of page area covers normal slide / dashboard /
        form / report colored fields but excludes signage and banner-
        sized colour blocks.
      - **Background luma above floor** (`bg_luma_floor`) — the chip's
        75th-percentile luma must be at least 60. Below that the chip is
        genuinely dark end-to-end and the rescue has nothing to lift to
        white; the binarizer handles it the same as any other dark
        region.
      - **Dark text density** inside the bbox bounded above by
        `max_dark_frac` — too many darks means a near-solid dark field
        (no text rescue possible) → leave alone. The lower bound is 0
        by default so PURELY decorative accents (a thin lime stripe
        with no text, a coloured callout dot) also whiten cleanly —
        on a 1-bit channel colour is dead anyway and a black bar
        is a worse outcome than blank.

    The dark threshold is **background-relative**: a pixel only counts
    as text-stroke if its grayscale value is at least `rel_dark_offset`
    luma below the chip's 75th-percentile luma (capped above by the
    absolute `dark_thr` floor so bright-chip AA halos still catch). An
    earlier absolute-only threshold rejected any chip whose own
    background luma sat below 110 (e.g. a dark-blue "EBITDA" badge at
    luma 108): the whole chip got counted as "text", `dark_frac`
    saturated, and the rescue refused to fire — the chip then went out
    as a solid-black knockout, which is the exact failure mode this
    routine exists to prevent. Anchoring the threshold to the chip's
    actual background fixes this for the full saturation range from
    pale-yellow chips down to navy badges.

    Whitening uses the COMPONENT'S BOUNDING BOX (padded outward by
    `pad`) minus the dark text strokes — not just a dilation of the
    chroma seed. Dilating the seed by a few pixels misses the soft
    outer fringe of an anti-aliased fill edge (where the chroma has
    already faded below `chroma_lo`); that residual mid-gray ring sits
    next to the now-pure-white field interior and the `contrast`
    binarizer's local-window statistics flip it to BLACK, drawing a
    phantom border around the rescued text. Whitening the whole
    bbox guarantees the halo is captured. The bbox is already capped
    by `max_area_frac`, and `~dark_pixels` keeps the actual glyphs.

    Returns a bool mask over the page; the caller bitwise-ANDs with
    `~photo_region` so the rescue never touches actual photo content."""
    h, w = gray.shape
    rgbf = rgb.astype(np.float32)
    r, g, b = rgbf[..., 0], rgbf[..., 1], rgbf[..., 2]
    mc = (r + g + b) / 3.0
    chroma = np.sqrt(((r - mc) ** 2 + (g - mc) ** 2 + (b - mc) ** 2) / 3.0)
    if chroma.max() < chroma_lo:
        return np.zeros((h, w), bool)

    # Per-pixel saturation gate then morph-close so the text strokes inside
    # the colored field (which sample the dark glyph colour, not the
    # saturated fill) don't punch holes in the detected region. Keep the
    # raw (pre-close) seed alive too — it's the only signal that
    # distinguishes "dark text on a solid coloured field" (raw seed dense
    # within the closed bbox) from "saturated-coloured text on white
    # paper" (raw seed = thin strokes, mostly white inside the closed
    # bbox). Without that gate the routine over-fires on light gold
    # decorative type and whitens the strokes themselves.
    raw_seed = (chroma > chroma_lo).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (block, block))
    seed = cv2.morphologyEx(raw_seed, cv2.MORPH_CLOSE, k)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(seed, 8)
    out = np.zeros((h, w), bool)
    max_area = max_area_frac * h * w
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area > max_area:
            continue
        x0 = stats[i, cv2.CC_STAT_LEFT]
        y0 = stats[i, cv2.CC_STAT_TOP]
        ww = stats[i, cv2.CC_STAT_WIDTH]
        hh = stats[i, cv2.CC_STAT_HEIGHT]
        comp = (labels[y0:y0 + hh, x0:x0 + ww] == i).astype(np.uint8)
        sub_gray = gray[y0:y0 + hh, x0:x0 + ww]
        n_comp = int(comp.sum())
        if n_comp < 16:
            continue
        # Background-relative dark threshold. An absolute floor like
        # dark_thr=110 fails on any saturated colour whose grayscale luma
        # itself sits below the floor — picture a dark-blue "EBITDA" chip
        # at RGB(65,107,223) (luma 108). The whole chip is then counted
        # as "dark text", dark_frac saturates near 1.0, exceeds
        # max_dark_frac, and the chip is rejected from the rescue. It
        # then falls through to the binarizer which paints it solid black
        # because the dark-text-on-dark-fill scenario is exactly what
        # this routine is supposed to protect against. The fix: anchor
        # the threshold to the chip's actual background luma. Use the
        # 75th percentile of the component's gray values as the
        # background estimate (robust to text strokes occupying up to a
        # quarter of the chip area), then call a pixel "text-dark" only
        # if it's `rel_dark_offset` luma below that background. The
        # absolute `dark_thr` floor is preserved as an upper bound so
        # bright chips still capture their anti-aliased text halos
        # (where pixel luma can be 90+) and don't slice the glyph
        # outline.
        chip_lumas = sub_gray[comp == 1]
        bg_luma = int(np.percentile(chip_lumas, 75))
        if bg_luma < bg_luma_floor:
            # Chip is genuinely dark all the way through; preserve_text
            # can't recover any text stroke here because there is no
            # background to lift to white. Let the normal binarizer
            # handle it (it'll go solid black, but there was never
            # anything to rescue).
            continue
        effective_dark_thr = max(rel_dark_floor,
                                 min(dark_thr, bg_luma - rel_dark_offset))
        # Dark-density gate. Too few darks = a plain colour swatch with no
        # text inside (a logo, a slide-deck accent block); too many = a
        # near-solid dark chip whose "text" is the chip itself. The middle
        # band is the text-on-colour signature.
        dark_pixels = (sub_gray < effective_dark_thr) & (comp == 1)
        n_dark = int(dark_pixels.sum())
        frac = n_dark / n_comp
        if not (min_dark_frac <= frac <= max_dark_frac):
            continue
        # Saturated-colour TEXT lookalike rejection. The component passed
        # the dark-density gate with frac near zero — this can be (a) a
        # solid decorative coloured accent on white (a chip, dot, stripe;
        # whiten it cleanly, the channel can't render colour anyway), OR
        # (b) the strokes of saturated-coloured TEXT on white paper (gold
        # logo subhead, coloured caption, tinted disclaimer text). For
        # case (b) the gold pixels themselves ARE the text and whitening
        # them destroys the line. The discriminator is RAW chroma density
        # inside the component's bounding box: a decorative accent fills
        # the bbox with saturated pixels end-to-end (density > 0.55), a
        # row of saturated-coloured glyphs leaves big paper-white gutters
        # between strokes (density < ~0.30). Only the dense case is a
        # genuine decorative accent we can erase safely.
        if frac < 0.005:
            raw_in_comp = raw_seed[y0:y0 + hh, x0:x0 + ww] & comp
            chroma_density = float(raw_in_comp.sum()) / max(1, n_comp)
            if chroma_density < solid_chroma_density:
                continue
        # Whiten the entire bounding box (with a small outer pad to absorb
        # the very last fringe of anti-aliasing past the chroma seed),
        # MINUS the dark text strokes. The bbox approach is far more
        # robust than dilating the seed: a 4-px dilation can't catch the
        # full soft halo of an anti-aliased colored fill edge — its
        # outermost pixels have already faded below `chroma_lo` so
        # they're not in the seed, but they're still mid-gray, and the
        # `contrast` text binarizer flips them to BLACK against the
        # surrounding white interior, painting a phantom border around
        # the rescued text.
        ybox0 = max(0, y0 - pad)
        xbox0 = max(0, x0 - pad)
        ybox1 = min(h, y0 + hh + pad)
        xbox1 = min(w, x0 + ww + pad)
        sub_dark_box = gray[ybox0:ybox1, xbox0:xbox1] < effective_dark_thr
        out[ybox0:ybox1, xbox0:xbox1] |= ~sub_dark_box
    return out


def _fill_component_holes(mask: np.ndarray) -> np.ndarray:
    """Fill ONLY background holes that are bounded by a single connected
    component of `mask` (uint8 0/1) — i.e. the sky/sign-patch case.

    The earlier implementation flood-filled from corner (0,0) and OR'd back
    every pixel it couldn't reach, which on a full-page wrapped raster fills
    in the gap between scattered variance specks (form table rows, body
    paragraph rows) and merges the whole page into one giant "photo" region.
    That dragged the document's own text into the halftone path and
    clobbered it.

    Instead we walk component-by-component, take each component's bounding
    box, and fill only the holes enclosed *within that bbox*. A form-table
    row sitting between two real photo components will not be filled,
    because no single component encloses it."""
    m = mask.astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = m.copy()
    for i in range(1, n):
        x = stats[i, cv2.CC_STAT_LEFT]
        y = stats[i, cv2.CC_STAT_TOP]
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]
        # Pad by 1 px so the flood seed is guaranteed to be background.
        sub = (labels[max(y - 1, 0):y + h + 1,
                      max(x - 1, 0):x + w + 1] == i).astype(np.uint8)
        inv = 1 - sub
        ff = np.zeros((sub.shape[0] + 2, sub.shape[1] + 2), np.uint8)
        cv2.floodFill(inv, ff, (0, 0), 0)
        holes = inv.astype(bool)
        out[max(y - 1, 0):y + h + 1, max(x - 1, 0):x + w + 1] |= holes
    return out.astype(bool)


def consolidate_photo_region(mask: np.ndarray, vdpi: int,
                             min_area_frac: float = 0.02) -> np.ndarray:
    """Turn a textured-photo mask into a solid photo *region*: close small gaps,
    fill interior holes within each photo component, and keep only large ones.

    This keeps flat areas that sit INSIDE a photo — a colored sign, a patch of sky
    — on the halftone path instead of binarizing them to stark white, while the
    page's own text-on-white areas (which lie outside the photo) stay excluded.
    Holes are filled per-component (see `_fill_component_holes`) so unrelated
    text rows between photo specks don't accidentally merge into a halftone."""
    k = max(15, int(round(vdpi / 8)))
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE,
                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = np.zeros_like(m)
    min_area = min_area_frac * mask.shape[0] * mask.shape[1]
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            out[labels == i] = 1
    return _fill_component_holes(out)


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


def deskew_gray(gray: np.ndarray, companions=None) -> tuple:
    """Estimate small skew from dark-pixel orientation and rotate to correct it.

    `companions` is an optional list of boolean masks rotated by the *same*
    transform (nearest-neighbour) so caller-side region masks stay aligned with
    the deskewed page. Returns (rotated_gray, angle, rotated_companions)."""
    companions = companions or []
    inv = 255 - gray
    coords = np.column_stack(np.where(inv > 64))
    if coords.shape[0] < 50:
        return gray, 0.0, companions
    angle = cv2.minAreaRect(coords[:, ::-1].astype(np.float32))[-1]
    if angle < -45:
        angle += 90
    if abs(angle) < 0.2 or abs(angle) > 15:
        return gray, 0.0, companions
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rot = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    comp_out = [cv2.warpAffine(c.astype(np.uint8), M, (w, h),
                               flags=cv2.INTER_NEAREST,
                               borderMode=cv2.BORDER_CONSTANT,
                               borderValue=0).astype(bool)
                for c in companions]
    return rot, float(angle), comp_out


def despeckle_bw(bw: np.ndarray, min_area: int = 2,
                 protect: np.ndarray | None = None) -> np.ndarray:
    """Remove isolated black specks (connected black components <= min_area px).

    bw: uint8 0/255 where 0 = black.
    `protect`: optional boolean mask where True = "do not touch this pixel"
        (typically the halftone region — a blue-noise dot is a 1-pixel
        connected component and is exactly what despeckling would erase).

    Vectorised: builds a per-component "is-tiny" lookup and applies it via a
    single fancy-index over the label array, so the cost is O(n_pixels) +
    O(n_components), not O(n_components × n_pixels). The old per-component
    `out[lbl == i] = 255` Python loop scanned the entire label array for
    every component — at 20 MP with millions of halftone dots that
    multiplies to *trillions* of operations and the run effectively never
    finishes."""
    black = (bw == 0).astype(np.uint8)
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(black, connectivity=8)
    if n <= 1:
        return bw
    areas = stats[:, cv2.CC_STAT_AREA]
    tiny = areas <= min_area
    tiny[0] = False                                 # label 0 is background
    flip = tiny[lbl]                                # per-pixel boolean
    if protect is not None:
        flip &= ~protect
    out = bw.copy()
    out[flip] = 255
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

    Text strokes are high-contrast and arranged in roughly horizontal runs. A
    top-hat / black-hat (morphological) response isolates light/dark structures
    while suppressing smooth photo gradients; we keep strong responses, group them
    into horizontal lines, and accept components whose geometry looks like a text
    line (wide, short, reasonably dense). The response is taken **multi-scale**
    (several stroke widths): body text is thin, but signage and display type have
    thick strokes whose interiors a single small kernel can't fill — it would catch
    only the edges, leaving the letters too sparse to pass the line-density gate
    (the failure that let big sign text fall through to the halftone). Still
    conservative: it would rather miss faint text than carve harsh blobs out of
    ordinary photo detail."""
    base = max(2, int(round(vdpi / 90)))          # ~ thinnest stroke scale (px)
    scales = sorted({base, base * 2, base * 4})   # thin body text … thick signage
    resp = np.zeros(gray.shape, np.float32)
    for sw in scales:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * sw + 1, 2 * sw + 1))
        r = np.maximum(cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k),   # dark text
                       cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k))     # light text
        resp = np.maximum(resp, r.astype(np.float32))
    strokes = (resp >= 45).astype(np.uint8)       # strong-contrast strokes only
    return _text_line_filter(strokes, vdpi, base)


def _text_line_filter(strokes: np.ndarray, vdpi: int, sw: int) -> np.ndarray:
    """Group a binary stroke map into accepted *text lines* and return the dilated
    glyph-body mask. Shared by the luminance detector (text_in_image_mask) and the
    chroma detector (washout_text_mask) so both apply the *same* conservative
    geometry gate: a component is kept only if it is wide, short and reasonably
    dense — the signature of a row of glyphs, not a photo edge or a color blob."""
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


# --------------------------------------------------------------------------- #
# OCR-driven text recolor — the unified text path                              #
# --------------------------------------------------------------------------- #
# The fax channel is luminance-only, so the contract for legible text is brutally
# simple: for every word on the page, paint its glyphs SOLID BLACK on a light/mid
# field or SOLID WHITE on a dark field — the #808080 polarity rule. We use OCR
# (rapidocr-onnxruntime) as a robust *word locator* (not as a typesetter): it
# finds where the words are, then we segment each word's ORIGINAL glyph pixels
# from the colour image and recolor *those exact pixels* per the rule, so the
# real letterforms are preserved (never retyped). The recoloured glyphs ride a
# text *layer* that sits ABOVE the halftoned image layer in the final composite,
# so the halftone screen can never disturb them.
#
# The same function handles both scopes: text OUTSIDE images (the document's
# header/footer/form text) and, when --recover-text is on, text INSIDE images
# (signage, captions). The only difference between the two calls is the region
# mask the OCR is scoped to.

def _ellipse(r: int):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _rel_luminance(rgb):
    """WCAG relative luminance of an sRGB colour (array last-axis = RGB, 0–255)."""
    c = np.asarray(rgb, np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def _wcag_contrast(rgb1, rgb2) -> float:
    """WCAG 2.x contrast ratio between two sRGB colours (1.0 … 21.0)."""
    l1, l2 = float(_rel_luminance(rgb1)), float(_rel_luminance(rgb2))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _segment_word(rgb: np.ndarray, gray: np.ndarray, quad: np.ndarray):
    """Use the OCR quad only to LOCATE a word, then segment its ORIGINAL glyph
    pixels (preserving the real letterforms). Returns (ink_full, bbox,
    field_gray, contrast) or None — *no* recoloring, so the caller can apply
    ONE consistent treatment across all the words on a sign.

    Strategy:
    1. **Tight field sampling.** Field colour comes from a thin ring *just
       outside* the OCR quad (a few percent of glyph height), so on small
       signs the ring still falls on the sign plate — never the surrounding
       photo, which would corrupt the field estimate and turn the segmenter
       into a blob detector.
    2. **Hysteresis threshold on LAB distance.** Otsu finds the natural valley
       between field and glyph clusters → a HIGH threshold marks "definite
       glyph" seeds (no AA halos, no shadows). A LOW threshold marks
       "candidate glyph" pixels including AA. The final ink mask is the
       connected components of the candidate set that touch a definite-glyph
       seed. Pixels at ambiguous distance only survive if they are part of a
       larger glyph stroke (region-growing), which preserves letterforms
       without inflating them.
    3. **Minimal cleanup.** A 1-pixel open removes specks; no close (a close
       welds adjacent letters together and fattens strokes — the very thing
       the user complained about)."""
    H, W = gray.shape
    q = np.asarray(quad, np.float32)
    bh = max(q[:, 1].max() - q[:, 1].min(), 1.0)
    pad = max(4, int(bh * 0.25))
    ring_w = max(2, int(round(bh * 0.15)))
    y0 = max(0, int(q[:, 1].min()) - pad); y1 = min(H, int(q[:, 1].max()) + pad)
    x0 = max(0, int(q[:, 0].min()) - pad); x1 = min(W, int(q[:, 0].max()) + pad)
    if y1 - y0 < 6 or x1 - x0 < 10:
        return None
    crgb = rgb[y0:y1, x0:x1]
    cgray = gray[y0:y1, x0:x1]
    hc, wc = cgray.shape
    lab = cv2.cvtColor(crgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    box = np.zeros((hc, wc), np.uint8)
    cv2.fillConvexPoly(box, (q - [x0, y0]).astype(np.int32), 1)
    box = box.astype(bool)
    if not box.any():
        return None
    quad_dilated = cv2.dilate(box.astype(np.uint8), _ellipse(ring_w))
    ring = quad_dilated.astype(bool) & ~box
    if not ring.any():
        ring = ~box
    field_lab = np.median(lab[ring].reshape(-1, 3), axis=0)
    dist = np.linalg.norm(lab - field_lab, axis=2)
    inbox = dist[box]
    if inbox.size < 16:
        return None

    # Otsu on the in-quad distance distribution: natural valley between field
    # cluster (low distance) and glyph cluster (high distance), regardless of
    # the glyph/field area ratio. The +6 floor guarantees we're a real distance
    # away from the field colour even on near-uniform crops where Otsu might
    # pick a tiny threshold and turn quantisation noise into ink.
    distu8 = np.clip(inbox, 0, 255).astype(np.uint8)
    otsu_thr, _ = cv2.threshold(distu8, 0, 255,
                                cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr_high = max(float(otsu_thr), 6.0)
    thr_low = max(thr_high * 0.55, 4.0)

    seed = (dist >= thr_high) & box
    cand = (dist >= thr_low) & box
    if not seed.any():
        return None

    # Hysteresis: keep only the connected components of `cand` that touch a
    # `seed` pixel. AA halos and shadow specks have low-distance noise but
    # no seed inside them, so they get dropped — letter strokes always have
    # a high-distance core, so they survive end-to-end.
    n_cc, labels = cv2.connectedComponents(cand.astype(np.uint8), 8)
    if n_cc <= 1:
        return None
    seed_labels = np.unique(labels[seed])
    seed_labels = seed_labels[seed_labels != 0]
    if seed_labels.size == 0:
        return None
    keep = np.zeros(n_cc, dtype=bool)
    keep[seed_labels] = True
    ink = keep[labels] & box

    # Drop isolated single-pixel specks the connected-component pass left in.
    if bh >= 12:
        ink = cv2.morphologyEx(
            ink.astype(np.uint8), cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))).astype(bool)

    cover = float(ink[box].mean())
    if ink.sum() < 12 or not (0.02 <= cover <= 0.55):
        return None
    field_gray = float(np.median(cgray[ring]))
    field_rgb = crgb[ring].reshape(-1, 3).mean(0)
    contrast = _wcag_contrast(crgb[ink].reshape(-1, 3).mean(0), field_rgb)
    ink_full = np.zeros((H, W), bool)
    ink_full[y0:y1, x0:x1] = ink
    return ink_full, (x0, y0, x1, y1), field_gray, round(contrast, 2)


def apply_ocr_polarity(rgb: np.ndarray, region_mask: np.ndarray | None,
                       conf_min: float = 0.5, scope: str = "image"):
    """Run OCR over `region_mask`, segment each word's ORIGINAL glyph pixels, and
    decide per-word polarity by the #808080 rule (median field luma < 128 → WHITE
    glyphs; ≥ 128 → BLACK glyphs). Used by BOTH text-handling passes:

    - scope="doc":   region_mask = pixels OUTSIDE images. This catches the page's
                     header/footer/form text, including white-on-coloured headers
                     a global Otsu threshold or naive binarizer can flip.
    - scope="image": region_mask = pixels INSIDE photo regions (recover-text). This
                     catches signage and captions baked into the photo.

    Returns (text_black, text_white, halftone_exclude, info). The two text
    masks are composited OVER the binarized + halftoned page; the
    `halftone_exclude` mask covers the *bbox area* of any word whose field is
    near-white (bg_luma ≥ 200), so the field around clean document text reads
    as a flat white plate instead of being halftoned (this is what saves doc
    text that the photo segmenter pulled into the image region by mistake).
    Words on a coloured field — signage at bg_luma ~140 — are left on the
    halftone path so the sign keeps its textured colour around the recoloured
    letters.

    OCR is optional: if `rapidocr-onnxruntime` isn't installed, the function
    returns empty masks and the rest of the pipeline still works (the binarizer
    handles plain document text on its own; only the OCR-driven recover path goes
    quiet). The polarity is decided **per sign** (words grouped by proximity)
    so all glyphs on one sign carry the same treatment."""
    import ocr_text
    H, W = rgb.shape[:2]
    info = {"scope": scope, "engine": ocr_text.engine_name(),
            "words_recognized": 0, "words_recolored_black": 0,
            "words_recolored_white": 0, "words": []}
    text_black = np.zeros((H, W), bool)
    text_white = np.zeros((H, W), bool)
    halftone_exclude = np.zeros((H, W), bool)
    if region_mask is None or not np.any(region_mask):
        return text_black, text_white, halftone_exclude, info

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    ys, xs = np.where(region_mask)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    crop = rgb[y0:y1, x0:x1]
    # Resample the crop to ~2600 px on its long edge before OCR. This is two
    # things at once: tiny crops are UPSCALED so the recogniser sees clean
    # text (rapidocr drops word spacing at small sizes); oversized crops from
    # high-DPI rasterization are DOWNSCALED so OCR's CPU cost stays bounded
    # (running detection on a 4000-px page is ~10× slower than on 2600 with
    # no measurable accuracy gain). Word quads come back at the resampled
    # scale and are mapped to full-resolution coordinates below, so glyph
    # segmentation still happens on the original pixels.
    hc, wc = crop.shape[:2]
    scale = max(0.5, min(2.0, 2600.0 / max(hc, wc, 1)))
    if abs(scale - 1.0) > 0.01:
        interp = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=interp)
    words = ocr_text.recognize(crop, conf_min)
    info["words_recognized"] = len(words)
    if not words:
        return text_black, text_white, halftone_exclude, info

    # Pass 1: keep only words whose centre falls inside the region, and segment
    # each word's ORIGINAL glyph pixels. No recolour yet.
    cands = []   # (text, conf, ink_full, bbox, field_gray, contrast)
    for text, quad, conf in words:
        quad = quad / scale + np.array([x0, y0], np.float32)
        iy = int(round(quad[:, 1].mean()))
        ix = int(round(quad[:, 0].mean()))
        if not (0 <= iy < H and 0 <= ix < W) or not region_mask[iy, ix]:
            continue
        seg = _segment_word(rgb, gray, quad)
        if seg is None:
            continue
        ink_full, bbox, field_gray, contrast = seg
        cands.append((text, conf, ink_full, bbox, field_gray, contrast))

    if not cands:
        return text_black, text_white, halftone_exclude, info

    # Group words into signs by proximity so co-located text gets one consistent
    # treatment (avoids painting "VILLA" black and "DEL" white on the same plate).
    bhs = [c[3][3] - c[3][1] for c in cands]
    link = max(8, int(np.median(bhs) * 0.9))
    gm = np.zeros((H, W), np.uint8)
    for c in cands:
        bx0, by0, bx1, by1 = c[3]
        gm[by0:by1, bx0:bx1] = 1
    gm = cv2.dilate(gm, _ellipse(link))
    _n, lbl = cv2.connectedComponents(gm, 8)
    groups: dict = {}
    for c in cands:
        bx0, by0, bx1, by1 = c[3]
        gid = int(lbl[(by0 + by1) // 2, (bx0 + bx1) // 2])
        groups.setdefault(gid, []).append(c)

    for grp in groups.values():
        # ONE field tone per sign (median across the group's per-word fields), then
        # apply the bright-line #808080 rule uniformly.
        sign_field = float(np.median([c[4] for c in grp]))
        polarity = "black" if sign_field >= POLARITY_THRESHOLD else "white"
        allink = np.zeros((H, W), bool)
        for c in grp:
            allink |= c[2]
        # Hybrid policy: the OCR pass DECIDES polarity but only RENDERS when it
        # actually adds something the binarizer/halftone wouldn't.
        #   - polarity="black" + field is light (≥200): SKIP painting glyphs.
        #     For doc scope the adaptive binarizer already renders this text at
        #     its real stroke weight — painting solid-black over it just
        #     thickens the segmenter's fatter ink mask. For image scope the
        #     near-white field also triggers `halftone_exclude` (below) which
        #     kicks the bbox out of the photo region, so the binarizer takes
        #     over there too; same reasoning applies.
        #   - polarity="white": always paint. The binarizer/halftone alone
        #     would put black ink on a dark field — wrong polarity, illegible.
        #   - polarity="black" + mid/dark field (<200): paint. On a tinted
        #     block (or a coloured sign), the binarizer's output is unreliable
        #     and the halftone can swallow the strokes.
        skip_render = polarity == "black" and sign_field >= 200.0
        if not skip_render:
            if polarity == "black":
                text_black |= allink
            else:
                text_white |= allink
        if polarity == "black":
            info["words_recolored_black"] += len(grp)
        else:
            info["words_recolored_white"] += len(grp)
        for text, conf, _ink, bbox, field_gray, contrast in grp:
            info["words"].append({
                "text": text, "conf": round(conf, 3),
                "polarity": polarity,
                "rendered": not skip_render,
                "field_gray": round(field_gray, 1),
                "wcag_contrast": contrast,
                "bbox": [int(bbox[0]), int(bbox[1]),
                         int(bbox[2]), int(bbox[3])]})
            # Where the word sits on a near-white field, mark its bbox so the
            # caller can subtract it from the halftone region. This rescues
            # doc-style text the photo segmenter pulled into the image scope:
            # the field reads as a clean white plate around the recoloured
            # glyphs instead of a halftone screen. Words on coloured fields
            # (signage, tinted bars) stay on the halftone path so the field
            # keeps its tone around the recoloured letters.
            if field_gray >= 200.0:
                bx0, by0, bx1, by1 = bbox
                bh = max(1, by1 - by0)
                pad = max(2, int(bh * 0.25))
                hx0 = max(0, bx0 - pad); hy0 = max(0, by0 - pad)
                hx1 = min(W, bx1 + pad); hy1 = min(H, by1 + pad)
                halftone_exclude[hy0:hy1, hx0:hx1] = True
    return text_black, text_white, halftone_exclude, info


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


# --------------------------------------------------------------------------- #
# Screen registry — single source of truth for the halftone library.          #
# --------------------------------------------------------------------------- #
# Every halftone schema registers one entry here. The dataclass carries the
# small pile of metadata each screen needs:
#
#   - `family`     : "am" (clustered cell) | "fm" (stochastic) | "ed" (error
#                    diffusion) | "line" (1-D / stripe) | "bilevel" (none).
#                    Used by the registry refactor itself + future
#                    `--compare-family` switching.
#   - `fax_safe`   : True iff the screen is eligible for `--dither auto` and
#                    the default `--compare-page` 6-up. Expressive screens
#                    (mezzotint, future spiral/glyph/etc.) set this False so
#                    they must be named explicitly.
#   - `gamma`      : Per-screen dot-gain pre-correction. The fax channel has
#                    large effective dot gain — without lifting midtones a
#                    photo plugs to a silhouette. Screens gain more than ED,
#                    so the correction is calibrated per family. Same
#                    semantics as the old _TONE_GAMMA dict (gamma<1 lifts).
#   - `info`       : One-line description for the contact-sheet caption and
#                    the `--list-dithers` style help.
#   - `aliases`    : Accepted --dither aliases that map to this canonical
#                    name. `clustered` carries `round` so `--dither round`
#                    is equivalent.
#
# `HALFTONE_INFO`, `_TONE_GAMMA`, and `DITHER_ALIASES` are *derived* from this
# dict below so adding a new screen is one line, not four out-of-sync edits.
@dataclass(frozen=True)
class Screen:
    family: str
    fax_safe: bool
    gamma: float
    info: str
    aliases: tuple = ()


SCREENS: dict[str, Screen] = {
    "none": Screen(
        "bilevel", True, 1.0,
        "Hard threshold — no halftone; correct for pure text, line art, and "
        "barcodes/QR codes.",
        ("threshold",),
    ),
    "clustered": Screen(
        "am", True, 0.62,
        "Clustered-dot AM screening — longest runs, best G4 compression, "
        "most robust over a noisy line; lowest apparent resolution.",
        ("round",),
    ),
    "screen": Screen(
        "am", True, 0.62,
        "Generalised AM dot screen — selectable spot function via "
        "--dot-shape {round|square|diamond|ellipse} and optional "
        "--screen-angle. `clustered` is the historical name for the round "
        "default; `screen --dot-shape square` is a crisp blocky look, "
        "`diamond` is the classic newspaper-photo aesthetic, and "
        "`ellipse` smooths midtone joins.",
        (),
    ),
    "ordered": Screen(
        "am", True, 0.66,
        "Bayer ordered dithering — fast, predictable crosshatch; "
        "middling on both detail and compression.",
        ("bayer",),
    ),
    "blue-noise": Screen(
        "fm", True, 0.70,
        "Void-and-cluster blue noise (FM) — isotropic organic stipple, "
        "great perceived detail, no directional worms; mid compression.",
        ("blue", "stipple"),
    ),
    "green-noise": Screen(
        "fm", True, 0.66,
        "Green-noise hybrid AM-FM — mid-size dot clusters: blue-noise "
        "detail with clustered-dot run-length/robustness. Tunable via "
        "coarseness; a strong default for a photo that must survive a "
        "bad line.",
        ("green",),
    ),
    "floyd": Screen(
        "ed", True, 0.85,
        "Floyd-Steinberg error diffusion — classic, maximum detail; "
        "directional speckle is the worst case for G4 size and line noise.",
        (),
    ),
    "atkinson": Screen(
        "ed", True, 0.82,
        "Atkinson error diffusion — clean whites, crisp thin features; "
        "good detail, looser compression than screening.",
        (),
    ),
    "jarvis": Screen(
        "ed", True, 0.85,
        "Jarvis-Judice-Ninke error diffusion — wide 12-tap kernel, very "
        "smooth tone; heavy speckle, large G4 size.",
        (),
    ),
    "stucki": Screen(
        "ed", True, 0.85,
        "Stucki error diffusion — 12-tap, sharp and smooth for print; "
        "heavy speckle, large G4 size.",
        (),
    ),
    "sierra": Screen(
        "ed", True, 0.85,
        "Sierra error diffusion — Jarvis-like smoothness, a little cheaper.",
        (),
    ),
    "edd": Screen(
        "ed", True, 0.82,
        "Edge-enhancing error diffusion — high-pass term sharpens edges "
        "while diffusing tone; for text over a photographic background.",
        (),
    ),
    "line": Screen(
        "line", True, 0.90,
        "Horizontal line screen (woodcut/engraving) — tone as horizontal "
        "stripes thickened by darkness; runs along the scanline so G4 size "
        "is excellent and it reads cleanly, high-contrast, never muddy. "
        "Optional --screen-angle for diagonal engraving (off-axis angles "
        "shorten runs and warn).",
        ("woodcut", "engraving", "lines"),
    ),
    "crosshatch": Screen(
        "line", True, 0.85,
        "Crosshatch / engraving — layered angled line screens lit by tone "
        "bands; reads as pen-and-ink etching. Default two perpendicular "
        "sets keep transition density near `line`; more --hatch-angles "
        "multiply transitions.",
        ("hatch", "etching"),
    ),
    "mezzotint": Screen(
        "am", False, 0.75,
        "Mezzotint random-threshold grain — unstructured stippling, "
        "velvety midtones. No spatial coherence → poor G4 compression and "
        "high line-noise sensitivity. Expressive: not eligible for `auto`.",
        ("grain",),
    ),
}


HALFTONE_INFO: dict[str, str] = {n: s.info for n, s in SCREENS.items()}
# `apply_tone_curve` reads this; only entries with a non-trivial gamma need
# to be present (the bilevel `none` screen has gamma=1.0 and is omitted so
# apply_tone_curve short-circuits to identity).
_TONE_GAMMA: dict[str, float] = {n: s.gamma for n, s in SCREENS.items()
                                 if s.gamma != 1.0}
# Accepted --dither aliases mapped to canonical names. Built from each
# screen's `aliases` tuple so adding a new alias is one line.
DITHER_ALIASES: dict[str, str] = {
    alias: name for name, s in SCREENS.items() for alias in s.aliases
}
FAX_SAFE_SCREENS: frozenset = frozenset(n for n, s in SCREENS.items() if s.fax_safe)


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
    """Identity passthrough since the pipeline now runs at SQUARE pixels (hdpi ==
    vdpi). Kept for back-compat with callers that still pass two DPIs."""
    if hdpi == vdpi:
        return tile
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


def _spot_function(shape: str, cell: int) -> np.ndarray:
    """Per-pixel "darkness rank" basis inside a single cell, before argsort.

    Lower values turn black first — i.e. the cluster origin. Each shape's
    growth front is one of these classic spot functions:

      - round:   Euclidean distance from centre   → round dot (the historical
                 `clustered` default; spiral growth).
      - square:  Chebyshev / max(|dx|,|dy|)       → square dot, blocky look.
      - diamond: Manhattan / |dx|+|dy|            → diamond dot, classic
                 newspaper photo.
      - ellipse: anisotropic Euclidean (a≠b)      → elliptical dot, smoother
                 midtone joins (chained dots vertically).
    """
    yy, xx = np.mgrid[0:cell, 0:cell].astype(np.float32)
    cx = cy = (cell - 1) / 2.0
    dx, dy = xx - cx, yy - cy
    if shape == "round":
        return np.sqrt(dx * dx + dy * dy)
    if shape == "square":
        return np.maximum(np.abs(dx), np.abs(dy))
    if shape == "diamond":
        return np.abs(dx) + np.abs(dy)
    if shape == "ellipse":
        # 1:1.7 aspect — visibly elongated without breaking tile boundaries.
        return np.sqrt(dx * dx + (dy / 1.7) ** 2)
    raise ValueError(f"unknown dot-shape: {shape!r}  (round|square|diamond|ellipse)")


def _screen_tile(shape: str, cell: int) -> np.ndarray:
    """Threshold tile (float32, 0..255) for an AM screen of the given shape and
    cell size. Identical computation to the historical `dither_clustered`
    for shape='round', so passing `--dither clustered` (or `--dither screen
    --dot-shape round` with the auto cell size) is byte-identical to the
    pre-registry output."""
    f = _spot_function(shape, cell)
    order = f.argsort(axis=None).argsort().reshape(cell, cell)
    return (order.astype(np.float32) + 0.5) / (cell * cell) * 255.0


def _auto_cell(vdpi: int) -> int:
    """Standard cell-size derivation for AM screens. Matches the historical
    `halftone()` rule for `clustered`, so back-compat is preserved."""
    return max(4, min(10, round(vdpi / 32)))


def dither_clustered(gray: np.ndarray, cell: int = 6, hdpi: int = 204,
                     vdpi: int = 196) -> np.ndarray:
    """Clustered-dot (AM) screening. Dots grow in a cluster, producing long runs
    that compress far better and survive a noisy line. `cell` is scaled from the
    fax dpi by the caller so the screen doesn't collapse after re-thresholding,
    and the tile is resampled to the device aspect so dots stay round on paper.

    Thin wrapper around `dither_screen(..., shape='round')` so the registry has
    one canonical screen implementation and `clustered` stays byte-identical
    to its pre-registry output."""
    return dither_screen(gray, hdpi=hdpi, vdpi=vdpi,
                         shape="round", angle=0.0, cell=cell)


def dither_screen(gray: np.ndarray, hdpi: int = 204, vdpi: int = 196,
                  shape: str = "round", angle: float = 0.0,
                  cell: int | None = None) -> np.ndarray:
    """Generalised AM dot screen.

    `shape` picks the spot function (round/square/diamond/ellipse — see
    `_spot_function`). `cell` defaults to the same DPI-scaled size used by
    `dither_clustered` so dot pitch stays consistent across the family.
    `angle` rotates the screen in image space: at 0° we tile the threshold
    block directly (fast, byte-identical to the historical clustered path);
    for non-zero angles we sample the threshold tile at rotated coordinates
    per pixel so the dot lattice tilts uniformly across the photo region.

    Off-axis angles trade off some G4 compression (shorter horizontal runs)
    for the classic 15°/45° angled-screen aesthetic. The threshold tile is
    NOT itself rotated — we rotate the sampling grid — which avoids the
    tile-seam artefact you get from rotating-then-tiling a small block.

    Non-round shapes need at least 5 cells to differ from `round`: at the
    4×4 minimum auto-cell, the Manhattan/Chebyshev/anisotropic-Euclidean
    rank orderings all collapse onto the same sequence (positions like
    (1,1) vs (0,2) only diverge once the grid is large enough to contain
    both). We bump the cell floor to 5 in that case so the shape choice
    actually shows up in the output. `round` keeps the historical cell=4
    floor so `--dither clustered` stays byte-identical to its pre-registry
    output at low render DPI.
    """
    if cell is None:
        cell = _auto_cell(vdpi)
        if shape != "round" and cell < 5:
            cell = 5
    thr = _aniso_tile(_screen_tile(shape, cell), hdpi, vdpi)
    th, tw = thr.shape
    if angle == 0.0:
        tiled = _tile_to(thr, gray.shape)
    else:
        h, w = gray.shape
        rad = float(np.deg2rad(angle))
        cosA, sinA = np.cos(rad), np.sin(rad)
        # Sample (x, y) in image space, rotate to the screen's local axes,
        # mod by the (anisotropic) tile period, look up the threshold rank.
        ys = np.arange(h, dtype=np.float32).reshape(h, 1)
        xs = np.arange(w, dtype=np.float32).reshape(1, w)
        u = xs * cosA + ys * sinA
        v = -xs * sinA + ys * cosA
        cu = np.mod(u, tw).astype(np.int32)
        cv = np.mod(v, th).astype(np.int32)
        tiled = thr[cv, cu]
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
                period: int = 0, angle: float = 0.0) -> np.ndarray:
    """Line-screen halftone (woodcut / engraving look).

    Tone is rendered as parallel stripes whose *thickness* grows with darkness:
    a triangular threshold profile across one period is high at the line center
    (so even highlights keep a hairline) and low at the edges (so only deep
    shadows fill the gap). At `angle=0.0` (the default) the lines run *along
    the scanline*, so the result is almost entirely long horizontal
    black/white runs — the single most G4-friendly way to carry a
    continuous-tone image over a fax line, while reading as a clean engraving
    rather than mud. The period is derived from the vertical DPI (so stripe
    pitch is constant on paper).

    `angle` rotates the stripes in image space. Diagonal engraving (15° or
    45°) reads as a classic copper-plate hatch, but the longer "runs along
    the scan-line" property dies off as the angle grows, so off-axis angles
    pay a real G4 cost — `recommend_dither`/`auto` therefore never picks an
    angled line screen, and the user passing one gets a warning emitted by
    the caller."""
    if period <= 0:
        # ~18 lines/inch: a bold, unmistakable woodcut pitch that survives both
        # the fax channel and being downscaled into a preview.
        period = max(4, int(round(vdpi / 18.0)))
    # Triangular threshold profile across one period. A high peak (250) keeps a
    # hairline even in highlights; a low edge (12) lets only deep shadows fill
    # the gap to near-solid. Black where gray <= thr, so the dark stripe THICKENS
    # smoothly as the local tone darkens — a line-conversion / engraving screen.
    center = (period - 1) / 2.0
    peak, edge = 250.0, 12.0
    if angle == 0.0:
        y = np.arange(period, dtype=np.float32)
        d = np.abs(y - center) / max(center, 1e-6)
        thr_period = peak - (peak - edge) * d
        tiled = _tile_to(thr_period.reshape(period, 1), gray.shape)
    else:
        # Rotated coord u = x·sinθ + y·cosθ; threshold profile is a function of
        # (u mod period). Sample per-pixel — fast and avoids the seam artefact
        # of rotating-then-tiling a 1-D profile.
        h, w = gray.shape
        rad = float(np.deg2rad(angle))
        sinA, cosA = np.sin(rad), np.cos(rad)
        ys = np.arange(h, dtype=np.float32).reshape(h, 1)
        xs = np.arange(w, dtype=np.float32).reshape(1, w)
        u = xs * sinA + ys * cosA
        u_mod = np.mod(u, period)
        d = np.abs(u_mod - center) / max(center, 1e-6)
        tiled = peak - (peak - edge) * d
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


def dither_crosshatch(gray: np.ndarray, hdpi: int = 204, vdpi: int = 196,
                      angles: tuple = (0.0, 90.0),
                      period: int = 0) -> np.ndarray:
    """Layered angled line screens, lit by tone bands — the pen-and-ink etching
    look. For each angle in `angles` we build a stripe field (same triangular
    profile as `dither_line`) whose stripe THICKNESS keys to a per-angle tone
    band: angle `i` fires once darkness exceeds `bands[i]` and grows from a
    hairline to a half-period stripe at full darkness. Layers are unioned, so
    midtones get one direction of hatch and deep shadows get the cross-hatch.

    Two angles at 0° and 90° (the default) give the most G4-friendly variant:
    horizontal runs survive for the upper-band layer, and the vertical layer
    only fires in the darker tail. Three or four angles read as a denser
    engraving but multiply the transition count along every scanline — the
    user is warned by the caller if `len(angles) > 2`.

    Wider stripe pitch than `dither_line` (~14 vs 18 lpi) so individual
    strokes read as visible pen lines rather than collapsing into smooth tone.
    """
    if period <= 0:
        period = max(4, int(round(vdpi / 14.0)))

    h, w = gray.shape
    # Per-pixel darkness in [0, 1].
    d_arr = 1.0 - gray.astype(np.float32) / 255.0
    half_period = period / 2.0
    n = max(1, len(angles))
    # Equally spaced band thresholds: the first stripe set fires earliest, the
    # last only in deep shadow. With n=2 → [1/3, 2/3] etc.
    bands = [(i + 1) / (n + 1) for i in range(n)]

    ys = np.arange(h, dtype=np.float32).reshape(h, 1)
    xs = np.arange(w, dtype=np.float32).reshape(1, w)

    ink = np.zeros((h, w), dtype=bool)
    for i, angle in enumerate(angles):
        rad = float(np.deg2rad(angle))
        sinA, cosA = np.sin(rad), np.cos(rad)
        u = xs * sinA + ys * cosA
        # Distance from the nearest line centre, normalised to half-period.
        u_mod = np.mod(u, period)
        dist_to_center = np.abs(u_mod - half_period)
        # Stripe radius grows from 0 (just past band threshold) to half_period
        # (fully black) — normalised so each band reaches max thickness at
        # darkness=1.0 regardless of where the band starts.
        band_lo = bands[i]
        excess = np.clip((d_arr - band_lo) / max(1.0 - band_lo, 1e-6), 0.0, 1.0)
        stripe_radius = excess * half_period
        ink |= (dist_to_center < stripe_radius)

    out = np.where(ink, 0, 255).astype(np.uint8)
    return out


# Mezzotint cache: each random-tile seed maps to a 256×256 rank matrix. The
# tile is uniform random — unlike blue-noise's void-and-cluster ranking, which
# enforces inter-pixel spacing — so the result is *unstructured* stippling.
_MEZZ_CACHE: dict[int, np.ndarray] = {}


def _mezzotint_tile(seed: int = 0) -> np.ndarray:
    if seed in _MEZZ_CACHE:
        return _MEZZ_CACHE[seed]
    rng = np.random.default_rng(int(seed))
    t = rng.permutation(256 * 256).reshape(256, 256).astype(np.int64)
    _MEZZ_CACHE[seed] = t
    return t


def dither_mezzotint(gray: np.ndarray, hdpi: int = 204, vdpi: int = 196,
                     seed: int = 0) -> np.ndarray:
    """Random-threshold (mezzotint) screening.

    A uniform-random rank tile turned into a threshold field, same code path
    as `dither_blue_noise` minus the void-and-cluster ordering. The result is
    velvety, unstructured grain — visually distinct from blue-noise FM
    (which is engineered to *avoid* low-frequency clumping). Because the
    ranks are uncorrelated spatially, the output has maximum 1-bit transition
    density per row, so G4 compression and line-noise robustness are both
    poor; this screen is expressive-tier and excluded from `auto`."""
    return _screen_from_matrix(gray, _mezzotint_tile(seed), hdpi, vdpi)


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

# `HALFTONE_INFO`, `_TONE_GAMMA`, `DITHER_ALIASES`, and `FAX_SAFE_SCREENS` are
# derived above from the SCREENS registry.


def _extract_photo_features(gray: np.ndarray,
                            mask: np.ndarray) -> dict:
    """Compute cheap content stats over photo-mask pixels for `recommend_dither`.

    Cost target: 10–30 ms per page. All ops are vectorised over the mask
    pixels only, so cost scales with photo area rather than page area.

    Returns an empty dict when the mask is too small to characterise
    (fewer than ~1000 pixels) — the caller falls back to the legacy
    photo-fraction-only logic in that case.

    Features:
      - mean_luma / std_luma                  0..255 stats on `gray[mask]`
      - dark_fraction / light_fraction         shadows (<80) / highlights (>200)
      - edge_density                           fraction of mask px with
                                               |dx|+|dy| > 20 (8-bit Sobel-lite
                                               via np.diff, no Sobel kernel)
      - texture_score                          mean(|grad|) / std_luma — proxy
                                               for fine-detail content (high =
                                               fine texture, low = smooth)
      - bimodal_score                          Otsu between-class variance
                                               normalised by total variance,
                                               0 = unimodal/smooth, 1 = perfectly
                                               bimodal (text/poster-like)
      - photo_area_px                          mask pixel count
      - n_regions                              connected-components count (8-conn)
    """
    if gray is None or mask is None:
        return {}
    m = (mask > 0)
    n = int(m.sum())
    if n < 1000:
        return {}

    pix = gray[m].astype(np.float32)
    mean_l = float(pix.mean())
    std_l = float(pix.std())

    dark_f = float((pix < 80).mean())
    light_f = float((pix > 200).mean())

    g16 = gray.astype(np.int16)
    dx = np.zeros_like(g16)
    dy = np.zeros_like(g16)
    dx[:, 1:] = np.abs(g16[:, 1:] - g16[:, :-1])
    dy[1:, :] = np.abs(g16[1:, :] - g16[:-1, :])
    grad = (dx + dy).astype(np.uint16)
    grad_in = grad[m]
    edge_density = float((grad_in > 20).mean())
    mean_grad = float(grad_in.mean())
    texture_score = mean_grad / std_l if std_l > 1e-6 else 0.0

    hist = np.bincount(pix.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total > 0 and std_l > 1e-6:
        p = hist / total
        idx = np.arange(256, dtype=np.float64)
        cum_p = np.cumsum(p)
        cum_mp = np.cumsum(idx * p)
        mu_t = cum_mp[-1]
        denom = cum_p * (1.0 - cum_p)
        with np.errstate(divide="ignore", invalid="ignore"):
            sigma_b = np.where(denom > 1e-6,
                               (mu_t * cum_p - cum_mp) ** 2 / denom,
                               0.0)
        var_total = float(std_l * std_l)
        bimodal_score = float(np.max(sigma_b) / var_total) if var_total > 0 else 0.0
        bimodal_score = max(0.0, min(1.0, bimodal_score))
    else:
        bimodal_score = 0.0

    n_regions = 0
    try:
        nr, _lbl = cv2.connectedComponents(m.astype(np.uint8), 8)
        n_regions = max(0, nr - 1)
    except Exception:
        n_regions = 0

    return {
        "mean_luma": round(mean_l, 1),
        "std_luma": round(std_l, 1),
        "dark_fraction": round(dark_f, 3),
        "light_fraction": round(light_f, 3),
        "edge_density": round(edge_density, 3),
        "texture_score": round(texture_score, 2),
        "bimodal_score": round(bimodal_score, 3),
        "photo_area_px": int(n),
        "n_regions": int(n_regions),
    }


def recommend_dither(photo_fraction: float, fax_heavy: bool,
                     vdpi: int = 196, *,
                     features: dict | None = None) -> tuple:
    """Suggest the OPTIMAL halftone for this page; returns (method, reason).

    Decision tree, in priority order:

      1. Page is essentially text/line art          -> none
      2. Low-DPI raster source (vdpi < 150)         -> blue-noise
      3. --fax-heavy mode                            -> clustered

    Below this, when feature stats are available, the picker discriminates
    within the detail-preserving family ({floyd, jarvis, edd, green-noise})
    plus the two high-contrast specialists ({clustered, atkinson}):

      4. Bimodal poster/signage (clean text + flat fields)   -> clustered
      5. Dark high-contrast photo (shadows + wide tonal std) -> atkinson
      6. Text-on-photo / strong edges at mid luma            -> edd
      7. Fine-detail texture (high mean-grad / std ratio)    -> floyd
      8. Smooth gradient (low std, low edges)                -> jarvis
      9. Mixed-content photo (no strong signal)              -> green-noise

    `recommend_dither` is signature-stable: callers that don't pass
    `features` get the legacy 4-branch behaviour and green-noise as the
    default for photo pages.
    """
    if photo_fraction < 0.03:
        return "none", ("Page is essentially text / line art (photo area "
                        f"{photo_fraction * 100:.0f}%); a hard threshold is the "
                        "sharpest and smallest — halftoning would only add noise.")
    if vdpi < 150:
        return "blue-noise", (f"Low-DPI raster source (~{vdpi} DPI render): "
                              "blue-noise FM stipple lays one dot per pixel with "
                              "no cell structure, so the screen looks fine and "
                              "organic where a clustered cell would collapse to "
                              "a chunky 25-lpi magazine pitch.")
    if fax_heavy:
        return "clustered", ("Fax-heavy mode: clustered-dot keeps runs long, so "
                             "it compresses best and survives a noisy line — at "
                             "the cost of a visibly coarse screen on fine photo "
                             "detail.")

    f = features or {}
    if f:
        mean_l = float(f.get("mean_luma", 128.0))
        std_l = float(f.get("std_luma", 0.0))
        edge_d = float(f.get("edge_density", 0.0))
        tex = float(f.get("texture_score", 0.0))
        bimod = float(f.get("bimodal_score", 0.0))
        dark_f = float(f.get("dark_fraction", 0.0))
        light_f = float(f.get("light_fraction", 0.0))
        polarity = dark_f + light_f

        # 4. True bimodal poster / signage: Otsu separability is high, the
        # tone histogram is dominated by shadows + highlights (low mid-tone
        # mass), and the interior is low-edge (sparse strokes, not text
        # overlaid on a photo). All three gates are required so this branch
        # only fires on genuine 2-tone artwork, not photos with strong
        # foreground/background separation.
        if (bimod > 0.70 and std_l > 70.0
                and polarity > 0.80 and edge_d < 0.12):
            return "clustered", (
                f"Bimodal poster/signage content (bimodality {bimod:.2f}, "
                f"std luma {std_l:.0f}, polarity {polarity:.2f}): clustered-"
                "dot resolves the two flat fields cleanly and compresses best, "
                "where an FM screen would add visible texture to areas the "
                "source treats as solid.")

        if mean_l < 85.0 and std_l > 50.0:
            return "atkinson", (
                f"Dark high-contrast photo (mean luma {mean_l:.0f}, "
                f"std luma {std_l:.0f}): Atkinson ED preserves thin whites and "
                "sparkle in deep shadows where green-noise plugs to mud and "
                "Floyd over-darkens.")

        # 6. Text-on-photo / strong-edge content: an edge-density gate at
        # ~15 % of the photo area picks up signage and overlay type sitting
        # on photographic backdrops (e.g., the Prestige cover billboard).
        if edge_d > 0.15 and 85.0 <= mean_l <= 200.0:
            return "edd", (
                f"Text-on-photo / strong-edge content (edge density "
                f"{edge_d * 100:.0f}%, mean luma {mean_l:.0f}): EDD's "
                "edge-enhancing diffusion keeps fine type readable against the "
                "photo backdrop better than any AM/FM screen.")

        if tex > 1.50:
            return "floyd", (
                f"Fine-detail texture (texture score {tex:.2f}): classic "
                "Floyd–Steinberg ED carries the highest fidelity for "
                "continuous-tone fine grain, at the cost of a chattier line "
                "than green-noise on a noisy channel.")

        if std_l < 30.0 and edge_d < 0.08:
            return "jarvis", (
                f"Smooth gradient (std luma {std_l:.0f}, edge density "
                f"{edge_d * 100:.0f}%): Jarvis–Judice–Ninke ED diffuses the "
                "tone the smoothest, so banding in skies / skin / paper "
                "gradients is softer than Floyd's tighter weights.")

        return "green-noise", (
            f"Mixed-content photo (mean luma {mean_l:.0f}, std luma "
            f"{std_l:.0f}, edge density {edge_d * 100:.0f}%, texture "
            f"{tex:.2f}): green-noise hybrid keeps blue-noise-level detail in "
            "continuous-tone regions while clustering dots into longer runs, "
            "the best balance when no single signal dominates.")

    return "green-noise", (f"Photo area {photo_fraction * 100:.0f}%: green-noise "
                           "hybrid keeps blue-noise-level detail in continuous-"
                           "tone regions while clustering dots into longer runs, "
                           "so it compresses and survives the line far better "
                           "than Floyd/Atkinson. Drop to atkinson for the "
                           "crispest whites on a clean line; switch to clustered "
                           "via --fax-heavy if you need maximum G4 compression.")


def choose_dither(name: str, fax_heavy: bool, photo_fraction: float,
                  vdpi: int = 196, *,
                  features: dict | None = None) -> str:
    if name and name != "auto":
        return DITHER_ALIASES.get(name, name)
    return recommend_dither(photo_fraction, fax_heavy, vdpi,
                            features=features)[0]


def halftone(gray: np.ndarray, name: str, hdpi: int, vdpi: int,
             coarseness: float = 4.0,
             dot_shape: str = "round",
             screen_angle: float = 0.0,
             hatch_angles: tuple = (0.0, 90.0),
             mezzotint_seed: int = 0) -> np.ndarray:
    """Dispatch a named halftone schema (see `SCREENS`).

    Extra parameters are screen-specific and have no effect on screens that
    don't read them:

      - `coarseness`     : green-noise AM↔FM knob (~2 detail … 8 robust).
      - `dot_shape`      : spot function for `screen` (round/square/diamond/
                           ellipse). `clustered` always uses round.
      - `screen_angle`   : screen rotation in degrees, used by `screen` and
                           `line`. Off-axis pays G4 cost — caller warns.
      - `hatch_angles`   : tuple of crosshatch angles. `len() > 2` warns.
      - `mezzotint_seed` : RNG seed for reproducible mezzotint output.
    """
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
        return dither_line(gray, hdpi=hdpi, vdpi=vdpi, angle=screen_angle)
    if name == "screen":
        return dither_screen(gray, hdpi=hdpi, vdpi=vdpi,
                             shape=dot_shape, angle=screen_angle)
    if name == "clustered":
        return dither_clustered(gray, cell=_auto_cell(vdpi),
                                hdpi=hdpi, vdpi=vdpi)
    if name == "crosshatch":
        return dither_crosshatch(gray, hdpi=hdpi, vdpi=vdpi,
                                 angles=hatch_angles)
    if name == "mezzotint":
        return dither_mezzotint(gray, hdpi=hdpi, vdpi=vdpi,
                                seed=mezzotint_seed)
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


def _compute_photo_region(page, gray, opt, hdpi, vdpi, rgb=None):
    """The continuous-tone (photo) region to halftone, before any text-keep masks
    are subtracted. Shared by OCR/recover-text scoping and the final mask.

    `rgb` (optional) feeds the colour-aware photo discriminator inside
    `variance_photo_mask` — for full-page wrapped rasters the chroma gate is
    what keeps grayscale form-table cells and body-text rows out of the
    halftone path (their variance alone is indistinguishable from a real
    photo). For grayscale sources the gate self-disables."""
    if opt.segmentation == "none":
        return np.zeros_like(gray, dtype=bool)
    if opt.segmentation == "variance":
        return variance_photo_mask(gray, rgb=rgb)
    mask = photo_region_mask(page, gray.shape, hdpi, vdpi,
                             opt.max_scanline_px, "embedded")
    if not mask.any():
        return variance_photo_mask(gray, rgb=rgb)
    if mask.mean() > 0.8:
        # A single image covering (nearly) the whole page is a full-page raster (a
        # scan or exported cover sheet), not a photo. Halftoning all of it would
        # dither the document's own text into mush. Find the genuine photo via the
        # variance heuristic and *consolidate* it into a solid region so flat areas
        # inside the photo (a colored sign, sky) keep halftoning while the document's
        # text-on-white areas, outside the photo, are binarized crisp. (Empty result
        # = all text/line art → binarize everything, correct for a text-only scan.)
        vmask = variance_photo_mask(gray, rgb=rgb)
        if vmask.any():
            vmask = consolidate_photo_region(vmask, vdpi)
        mask = mask & vmask
    return mask


def _prepare_page(page: fitz.Page, opt: FaxOptions):
    """Rasterize a page at the effective (square) DPI, segment image areas, run
    OCR in each scope to build the BLACK/WHITE text masks, and pre-clean the
    grayscale.

    Returns (gray, mask, photo_fraction, warnings, already_bilevel, doc_info,
    image_info, text_black, text_white, eff_dpi). `mask` is the photo region the
    halftone runs over; `text_black` and `text_white` are the layered text masks
    composited over the bilevel result in `_apply_dither` so the halftone never
    disturbs them; `eff_dpi` is the actual rendered DPI (single source of truth
    for halftone parameters).

    The result is reused by both the real conversion and the multi-method
    comparison so every halftone option starts from an identical state."""
    # The effective DPI is the SINGLE source of truth for both the rendered
    # pixel grid AND every per-pixel mask we build on top of it. Passing the
    # preset DPI (e.g. 391 for superfine) instead would scale the embedded
    # image rectangles at the preset's scale and then stamp them into a buffer
    # that `render_page_color` actually sized at the *effective* DPI (the
    # MAX_RENDER_DPI ceiling, or whatever native DPI a raster source carries
    # below it) — the rectangles land at the wrong size and the photo mask
    # covers only a sub-rectangle of each image, so e.g. the top-left quadrant
    # of an embedded photo gets halftoned and the rest gets hard-binarized
    # into blocky shapes.
    eff_dpi = _page_effective_dpi(page, opt)
    warnings: list = []
    doc_info: dict = {}
    image_info: dict = {}

    if is_already_bilevel(page):
        gray = render_page_gray(page, eff_dpi, eff_dpi, opt.max_scanline_px)
        empty = np.zeros_like(gray, dtype=bool)
        return (gray, empty, 0.0, warnings, True, doc_info, image_info,
                empty, empty, eff_dpi)

    # Always render in colour: OCR runs on the colour image. The colour buffer is
    # never recoloured in place — the OCR pass produces glyph MASKS that ride a
    # text layer composited above the halftoned image layer in the final assembly.
    rgb = render_page_color(page, eff_dpi, eff_dpi, opt.max_scanline_px)
    base_gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    # The photo region scopes OCR and the final halftone mask. Compute it on a
    # flattened copy (near-white → white) so the document's text-on-white areas
    # read as flat and stay OUT of the photo region — otherwise the page's own
    # form text would be halftoned. It rides the deskew alongside the text masks.
    photo = _compute_photo_region(
        page, flatten_background(base_gray) if opt.flatten_bg else base_gray,
        opt, eff_dpi, eff_dpi, rgb=rgb)

    # Preserve-text rescue. Find small saturated-colour fields outside the
    # photo region that contain dark text strokes (highlight chips, badges,
    # callouts, colored cells, filled banners, …), and lift them to white
    # in BOTH the gray (for the binarizer) and the RGB (so OCR, if it runs
    # later, sees the same field tone and skips painting white glyphs on
    # what's now a light-field word). This is the cure for the slide-4
    # failure mode: a label "Active accounts" on a saturated lime chip
    # collapses to gray=140-ish where the contrast binarizer flips polarity
    # and knocks the glyphs out as a mangled crosshatch. After whitening,
    # the same text reads as clean black-on-white through the standard
    # binarize path. Photo content is protected via `~photo`; large
    # coloured panels are protected via the `max_area_frac` filter inside
    # `preserve_text_mask` (slide 6's lime backdrop keeps its
    # halftone).
    if opt.preserve_text and rgb is not None and rgb.ndim == 3:
        whiten = preserve_text_mask(rgb, base_gray)
        whiten &= ~photo
        if whiten.any():
            base_gray = base_gray.copy()
            base_gray[whiten] = 255
            rgb = rgb.copy()
            rgb[whiten] = 255
            kpx = int(whiten.sum() / 1000)
            warnings.append(f"text_preserved:{kpx}kpx")

    text_black = np.zeros(base_gray.shape, bool)
    text_white = np.zeros(base_gray.shape, bool)
    halftone_exclude = np.zeros(base_gray.shape, bool)

    def _count_rendered(info):
        return sum(1 for w in info.get("words", []) if w.get("rendered"))

    # OCR is the slow step (an inference pass per page). It is the engine for
    # *both* the doc-text polarity recolour AND the within-image recover-text
    # recolour, so it only ever runs when the user has opted into the
    # recover-text feature (`opt.recover_text != "off"`). With recover-text
    # off the chroma-aware photo segmenter routes form/body/footer text out of
    # the halftone path on its own, and the binarizer's adaptive contrast
    # threshold handles polarity inside white-paper text — so the OCR pass
    # would be pure overhead (20+ minutes on a 6-page 391-DPI deck).
    if opt.recover_text != "off":
        # (1) Document text — pixels OUTSIDE the photo region. Apply the
        # #808080 polarity rule to the page's own header/footer/form text:
        # white-on-coloured bars, body text on tinted blocks, etc.
        if opt.ocr_text != "off":
            non_image = ~photo
            d_black, d_white, d_excl, doc_info = apply_ocr_polarity(
                rgb, non_image, opt.ocr_conf_min, scope="doc")
            text_black |= d_black
            text_white |= d_white
            halftone_exclude |= d_excl
            n_rend = _count_rendered(doc_info)
            if n_rend:
                warnings.append(f"doc_text:recolored:{n_rend}")

        # (2) Robust image text — pixels INSIDE the photo region. Same
        # #808080 rule, rescuing signage and captions baked into images.
        i_black, i_white, i_excl, image_info = apply_ocr_polarity(
            rgb, photo, opt.ocr_conf_min, scope="image")
        text_black |= i_black
        text_white |= i_white
        halftone_exclude |= i_excl
        n_rend = _count_rendered(image_info)
        if n_rend:
            warnings.append(f"image_text:recolored:{n_rend}")

        # (3) Per-glyph LOCAL STAGE. Replace the gray plane in a thin
        # halo-sized envelope around each recovered glyph's silhouette
        # (NOT the full word bbox) so the halftone paints a smooth screen
        # only where the letterform + contrasting-glow halo actually sits.
        # The rest of the bbox keeps its halftone, so the underlying photo
        # (billboard panel, sign, building) keeps its natural texture
        # instead of being replaced wholesale by a uniform gray plate.
        #
        # Why the envelope and not the bbox: OCR returns a phrase-level
        # bbox per word ("VILLA DEL SOL"), which on a wide sign covers
        # almost the entire panel. Flattening the bbox = flattening the
        # whole sign. Flattening a glyph-dilation = a thin smooth-tone
        # silhouette that hugs the letters and gives the halo a clean
        # background, while the gaps between letters and the area past
        # the halo retain the photo's halftone.
        #
        # Envelope radius scales with vdpi so the visual margin past the
        # halo (`glow_px = vdpi/100`) is constant on paper.
        if image_info and image_info.get("words"):
            glyph = i_black | i_white
            if glyph.any():
                glow_px = max(1, int(round(eff_dpi / 100)))
                # halo radius + 2 px breathing room. Tight, but enough that
                # the halo always lands on flattened pixels.
                stage_r = glow_px + 2
                k = stage_r * 2 + 1
                stage_kernel = cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE, (k, k))
                stage = cv2.dilate(glyph.astype(np.uint8),
                                   stage_kernel).astype(bool)
                base_gray_flat = base_gray.copy()
                n_flat = 0
                for w in image_info["words"]:
                    if not w.get("rendered"):
                        continue
                    bx0, by0, bx1, by1 = w["bbox"]
                    sub_stage = stage[by0:by1, bx0:bx1]
                    if not sub_stage.any():
                        continue
                    base_gray_flat[by0:by1, bx0:bx1][sub_stage] = \
                        int(round(w["field_gray"]))
                    n_flat += 1
                if n_flat:
                    base_gray = base_gray_flat
                    warnings.append(f"image_text:field_flattened:{n_flat}")

    # Words on a near-white field — typically doc text the photo segmenter
    # pulled into the image scope — are removed from the halftone region so
    # the field around them reads as a flat white plate. Coloured-field words
    # (signage at field_gray < 200) stay on the halftone path so the sign keeps
    # its tone.
    if halftone_exclude.any():
        photo = photo & ~halftone_exclude

    gray = base_gray

    if opt.deskew:
        gray, ang, (photo, text_black, text_white) = deskew_gray(
            gray, [photo, text_black, text_white])
        if ang:
            warnings.append(f"deskew:{ang:.1f}deg")
    if opt.flatten_bg:
        gray = flatten_background(gray)

    mask = photo
    photo_fraction = float(mask.mean()) if mask.size else 0.0
    features = _extract_photo_features(gray, mask) if photo_fraction > 0 else {}
    return (gray, mask, photo_fraction, warnings, False, doc_info, image_info,
            text_black, text_white, eff_dpi, features)


def _apply_dither(gray, mask, dither_name, opt, hdpi, vdpi,
                  text_black=None, text_white=None) -> np.ndarray:
    """Build the bilevel page in three passes:

    1. **Image layer** — halftone (or hard-threshold) inside the photo `mask`,
       with optional dot-gain pre-correction and edge sharpening so photos read
       cleanly through the channel.
    2. **Document layer** — adaptive binarization for everything outside the
       photo mask, so vector/document text stays crisp solid black.
    3. **Text layer (composite ABOVE the others)** — `text_black` pixels are
       forced solid black, `text_white` pixels solid white. This is the OCR-
       driven recolor: the halftone screen never disturbs glyphs because they
       are painted on top.

    Pass `text_black=None`/`text_white=None` to skip the text layer (used by the
    multi-panel sample's "halftone-only" frame)."""
    # Low-DPI raster sources (a 792x1024 wrapped PNG renders at ~72 DPI) bake
    # text as anti-aliased pixels, not vector glyphs — a thin stroke's edge
    # pixels sit in the binarizer's grey-zone and either get dropped (the
    # stroke vanishes, "B" reads as "8") or fragment (a "5" splits into two
    # components and reads as "S"). A mild unsharp mask BEFORE binarization
    # pushes those edge pixels toward solid black or solid white so the
    # binarizer sees crisp edges. Only the text-binarize path is sharpened;
    # the photo-region gray passed to the halftone is untouched, since
    # `pre_sharpen` already runs there under `opt.sharpen` with its own knob.
    gray_for_text = pre_sharpen(gray, vdpi, amount=1.2) if vdpi < 150 else gray
    text_bw = binarize_text(gray_for_text, opt.text_binarize, vdpi)
    dither_name = DITHER_ALIASES.get(dither_name, dither_name)
    bw = text_bw.copy()

    if mask.any() and dither_name != "none":
        ys, xs = np.where(mask)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        sub = gray[y0:y1, x0:x1]
        if opt.sharpen:
            sub = pre_sharpen(sub, vdpi)
        sub = apply_tone_curve(sub, dither_name, opt.tone_curve)
        sub_ht = halftone(sub, dither_name, hdpi, vdpi,
                          coarseness=opt.green_noise_coarseness,
                          dot_shape=opt.dot_shape,
                          screen_angle=opt.screen_angle,
                          hatch_angles=opt.hatch_angles)
        # Heuristic text-in-image rescue runs ONLY when recover-text is on. The
        # default-mode contract is "image content rendered as-is" — the
        # billboard, signage, and captions inside a photo are halftoned along
        # with the rest of the picture, no special handling. Recover-text is
        # the user-opt-in switch that turns on text recovery inside images;
        # this stroke-detector backs up the OCR pass for any words it missed.
        if opt.text_in_image and opt.recover_text != "off":
            tmask = text_in_image_mask(gray[y0:y1, x0:x1], vdpi)
            if tmask.any():
                sub_ht = np.where(tmask, text_bw[y0:y1, x0:x1], sub_ht)
        sub_mask = mask[y0:y1, x0:x1]
        region = bw[y0:y1, x0:x1]
        region[sub_mask] = sub_ht[sub_mask]
        bw[y0:y1, x0:x1] = region

    # Composite the OCR-driven text layer ABOVE the halftone/binarize layers,
    # each glyph wrapped in a CONTRASTING-GLOW halo of opposite polarity so
    # the letterforms read against ANY underlying tone. The halo costs only
    # the dilation ring around each stroke and is invisible when the
    # underlying field already matches the halo polarity (black text on a
    # light page: the white halo merges into the page; white text on a dark
    # halftone field: the black halo creates a sharp keyline that makes the
    # white stroke pop). It replaces the older "lift the whole bbox to
    # opposite polarity" path that knocked out hundreds of kilopixels of
    # surrounding image content on signage (a teal billboard with gold text
    # would lose its entire panel to one big white rectangle). The halo
    # gives the same legibility for ~1% of the pixels.
    #
    # Halo + stroke thickness both SCALE WITH RENDER DPI so the visual
    # weight on paper (≈0.5 pt outline) is constant across resolutions
    # — a fixed 3×3 kernel that looks fine at 110 PPI disappears at
    # 300 PPI, which is why the v3 billboard composite "looked terrible"
    # on the hi-res preview earlier.
    glow_px = max(1, int(round(vdpi / 100)))         # 1 px @ 100 DPI, 3 px @ 300 DPI
    halo_k = glow_px * 2 + 1
    halo_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (halo_k, halo_k))
    # Recovered glyph strokes are 1-px-thin at low DPI sources; the channel
    # eats them. Dilate by 1 px at <200 DPI to give the stroke survival
    # margin (matches the existing `--thicken` rationale).
    thicken_strokes = vdpi < 200
    if thicken_strokes:
        thick_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    if text_black is not None and text_black.any():
        tb = text_black
        if thicken_strokes:
            tb = cv2.dilate(tb.astype(np.uint8), thick_kernel).astype(bool)
        halo = cv2.dilate(tb.astype(np.uint8), halo_kernel).astype(bool) & ~tb
        bw[halo] = 255
        bw[tb] = 0
    if text_white is not None and text_white.any():
        tw = text_white
        if thicken_strokes:
            tw = cv2.dilate(tw.astype(np.uint8), thick_kernel).astype(bool)
        halo = cv2.dilate(tw.astype(np.uint8), halo_kernel).astype(bool) & ~tw
        bw[halo] = 0
        bw[tw] = 255
    return bw


def process_page(page: fitz.Page, idx: int, opt: FaxOptions) -> tuple[Image.Image, PageReport]:
    rep = PageReport(index=idx)
    if opt.basic:
        # BARE-MINIMUM PATH. Render at the page's effective DPI, desaturate, and
        # Otsu-threshold. No MRC, no OCR, no flatten_bg, no deskew, no despeckle,
        # no thicken — exactly the steps required to land 1-bit pixels in a G4
        # frame, and nothing else. Provided as an escape hatch for users who
        # want a predictable, debuggable, "just a fax" output, and as a
        # reference baseline against which the full pipeline's enhancements can
        # be visually compared. transmission_safe (the 1728-px clamp) still
        # applies, because that's a hard channel constraint rather than an
        # enhancement.
        eff_dpi = _page_effective_dpi(page, opt)
        gray = render_page_gray(page, eff_dpi, eff_dpi, opt.max_scanline_px)
        bw = threshold_otsu(gray)
        rep.dither = "none"
        rep.text_binarize = "otsu"
        rep.photo_fraction = 0.0
        return _finalize_basic(bw, opt), rep
    (gray, mask, photo_fraction, warnings, already, doc_info, image_info,
     text_black, text_white, eff_dpi, photo_features) = _prepare_page(page, opt)
    rep.warnings.extend(warnings)
    rep.photo_fraction = round(photo_fraction, 4)
    rep.text_binarize = opt.text_binarize
    rep.photo_features = photo_features
    if image_info.get("words"):
        rep.recover_text = image_info
    if doc_info.get("words"):
        rep.ocr_text = doc_info

    if already:
        rep.already_bilevel = True
        rep.dither = "none"
        return _finalize(threshold_otsu(gray), rep, opt), rep

    rep.photo_regions = int(mask.any())
    dname = choose_dither(opt.dither, opt.fax_heavy, photo_fraction, eff_dpi,
                          features=photo_features)
    rep.dither = dname
    rep.warnings.extend(_screen_choice_warnings(dname, opt))
    bw = _apply_dither(gray, mask, dname, opt, eff_dpi, eff_dpi,
                       text_black=text_black, text_white=text_white)

    if (bw == 0).mean() > 0.45:
        rep.warnings.append("inverted_or_heavy_black")
    rep.warnings.extend(detect_washout_colors(page))
    return _finalize(bw.astype(np.uint8), rep, opt, protect=mask), rep


def _screen_choice_warnings(dither_name: str, opt: "FaxOptions") -> list:
    """Honest-cost warnings for screen choices that compromise the channel.

    A user can name an expressive screen or stack an off-axis angled line on
    top of `screen`/`line`; the pipeline will produce it, but the JSON report
    should call out that the result may not survive a real fax line. Same
    style as `inverted_or_heavy_black` / `wash_out_color:*` — terse, keyed
    strings the caller can grep.
    """
    warns: list = []
    name = DITHER_ALIASES.get(dither_name, dither_name)
    info = SCREENS.get(name)
    if info is not None and not info.fax_safe:
        warns.append(f"expressive_screen:{name}")
    if name in ("screen", "line") and abs(opt.screen_angle) > 15.0:
        warns.append(f"off_axis_screen:{name}:{opt.screen_angle:.0f}deg")
    if name == "crosshatch" and len(opt.hatch_angles) > 2:
        warns.append(f"crosshatch_dense:{len(opt.hatch_angles)}_angles")
    return warns


def _postclean(bw: np.ndarray, opt: FaxOptions,
               protect: np.ndarray | None = None) -> np.ndarray:
    if opt.despeckle:
        bw = despeckle_bw(bw, protect=protect)
    if opt.thicken:
        bw = thicken_bw(bw)
    return bw


def _finalize(bw: np.ndarray, rep: PageReport, opt: FaxOptions,
              protect: np.ndarray | None = None) -> Image.Image:
    """Clean specks/thicken hairlines, optionally clamp to a Group-3 scanline
    (1728 px) so a real fax machine can transmit it, and pack as a 1-bit image.

    `protect`, if supplied, is the halftone region — single-pixel FM dots are
    valid halftone content (a blue-noise screen IS a field of 1-px dots) and
    must never be despeckled away."""
    cleaned = _postclean(bw, opt, protect=protect)
    img = Image.fromarray(cleaned).convert("1")
    if opt.transmission_safe and img.width > 1728:
        ratio = 1728 / img.width
        new_h = max(1, int(round(img.height * ratio)))
        img = img.convert("L").resize((1728, new_h), Image.LANCZOS)
        img = img.point(lambda v: 0 if v < 160 else 255).convert("1")
    return img


def _finalize_basic(bw: np.ndarray, opt: FaxOptions) -> Image.Image:
    """Pack a 1-bit image — no postclean, no enhancement. Honours
    transmission_safe (a hard channel constraint, not an enhancement)."""
    img = Image.fromarray(bw).convert("1")
    if opt.transmission_safe and img.width > 1728:
        ratio = 1728 / img.width
        new_h = max(1, int(round(img.height * ratio)))
        img = img.convert("L").resize((1728, new_h), Image.LANCZOS)
        img = img.point(lambda v: 0 if v < 160 else 255).convert("1")
    return img


# --------------------------------------------------------------------------- #
# Encoding / packing                                                          #
# --------------------------------------------------------------------------- #
def encode_g4_tiff(img: Image.Image, path: str, dpi=None) -> int:
    """Save a 1-bit page as a CCITT Group-4 TIFF. The (square) effective DPI is
    written into the TIFF resolution tags so img2pdf — and any TIFF reader —
    builds a correctly-proportioned PDF page. Without it img2pdf would assume a
    72-dpi default and stretch the page."""
    kw = {"format": "TIFF", "compression": "group4"}
    if dpi is not None:
        kw["dpi"] = (float(dpi[0]), float(dpi[1]))
    img.save(path, **kw)
    return os.path.getsize(path)


def convert_pdf(in_pdf: str, out_path: str, opt: FaxOptions,
                progress=None) -> dict:
    """Convert every page and write a G4 PDF or a Class-F multipage TIFF.

    Processes pages STRICTLY one at a time: render → bilevel → encode page to
    a G4 TIFF on disk → release the in-memory image → garbage-collect →
    next page. Only the page's report dict survives the loop iteration; the
    multi-megapixel intermediates (RGB buffer, gray buffer, photo/text masks,
    PIL image) are dropped before the next page starts, so memory stays flat
    at one page's worth even for a 100-page deck at the 300-PPI cap. After every
    page is encoded, img2pdf concatenates the per-page TIFFs into the final
    multipage PDF (or PIL writes the multipage TIFF), without ever loading
    more than one page's pixels into RAM at a time.

    `progress(i, n_pages, rep)` is called after each page is written, if
    supplied — primarily so the CLI can stream status while a long deck
    runs, but also handy for any caller that wants a progress bar.

    Returns the consolidated report dict."""
    import gc
    doc = fitz.open(in_pdf)
    n_pages = doc.page_count
    tmpdir = tempfile.mkdtemp(prefix="faxopt_")
    tiff_paths, pages = [], []

    for i in range(1, n_pages + 1):
        page = doc[i - 1]
        img, rep = process_page(page, i, opt)
        tp = os.path.join(tmpdir, f"p{i:04d}.tif")
        # Effective DPI from actual pixels ÷ page inches, so the PDF carries the
        # source aspect ratio regardless of which preset was used.
        inch_w, inch_h = page.rect.width / 72.0, page.rect.height / 72.0
        dpi = ((img.width / inch_w, img.height / inch_h)
               if inch_w > 0 and inch_h > 0 else None)
        nbytes = encode_g4_tiff(img, tp, dpi=dpi)
        rep.encoded_bytes = nbytes
        rep.est_transmission_s = round(
            nbytes * 8 / opt.line_rate_bps + opt.page_overhead_s, 1)
        tiff_paths.append(tp)
        pages.append(rep)
        if progress is not None:
            progress(i, n_pages, rep)
        # Drop the in-memory page image before the next page starts. PyMuPDF's
        # per-page caches are also reset by re-fetching the page object on
        # each iteration above (instead of holding an iterator).
        del img
        gc.collect()

    if opt.fmt == "tiff":
        _save_multipage_tiff(tiff_paths, out_path)
    else:
        # img2pdf embeds CCITT G4 losslessly (PDF carries CCITTFaxDecode).
        # It streams each TIFF from disk page by page, never holding the
        # whole deck's bilevel pixels in memory.
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
    kw = dict(format="TIFF", compression="group4",
              save_all=True, append_images=rest)
    dpi = first.info.get("dpi")              # carry the per-page DPI through
    if dpi:
        kw["dpi"] = dpi
    first.save(out_path, **kw)


# --------------------------------------------------------------------------- #
# Unified contact-sheet ("--sample") infrastructure                           #
# --------------------------------------------------------------------------- #
#
# `render_contact_sheet()` is the single entry point for every per-page panel
# comparison the skill emits. It supersedes the previously separate
# `render_sample` (4-panel pipeline-stages view), `render_comparison`
# (6-up dither comparison), `render_preview` (1-panel), and
# `render_recover_text_preview` (2-panel before/after) functions; those remain
# as thin shims for backward compatibility with existing callers and CLI flags.
#
# The function takes one knob — `panel_count` — that selects from PANEL_RECIPES
# below, where each recipe is a list of CONTENT KEYS describing what goes in
# each cell. Content keys come in five flavours:
#
#   - "orig"          → the source page in colour (no fax processing)
#   - "gray"          → the source page desaturated (no fax processing)
#   - "default_fax"   → `--basic` mode (grayscale + Otsu, no MRC / OCR /
#                       halftone) — the "naive fax" baseline
#   - "optimized"     → the user's full options as passed in
#   - "recommended"   → the user's options BUT with dither replaced by the
#                       auto-picker's recommendation (so a 2-up of orig +
#                       recommended is always "what Claude would pick")
#   - "<dither>"      → the user's options with dither overridden. Plain
#                       names ("floyd", "blue-noise", etc.) map to the
#                       matching SCREENS entry; parametric aliases
#                       "screen-square", "screen-diamond", "screen-ellipse"
#                       map to dither="screen" + the corresponding dot_shape.
#
# Canonical ordered catalogue of every dither key the user-facing contact
# sheet exposes. Matches the SCREENS registry but adds the three parametric
# `screen` variants (square / diamond / ellipse dot) as discrete keys so
# they appear as individual panels in the 20-up sheet and in the
# README halftone gallery. This is the SINGLE SOURCE OF TRUTH — both
# `PANEL_RECIPES[20]` below AND `_run_readme_demo.py:build_halftone_grid`
# read this list, so adding a new screen here automatically lights it up
# everywhere.
ALL_HALFTONE_KEYS: list[str] = [
    "none", "clustered",
    "screen-square", "screen-diamond", "screen-ellipse",
    "ordered", "blue-noise", "green-noise",
    "floyd", "atkinson", "jarvis", "stucki", "sierra", "edd",
    "line", "crosshatch", "mezzotint",
]

# PANEL_RECIPES is monotonic by design: each higher panel-count is a strict
# superset of the elements you'd see at the lower counts, so the user's choice
# of `--panels K` is a clean detail dial. The 20-up mirrors
# `ALL_HALFTONE_KEYS` above, so a user running `--sample 1 --panels max`
# gets the same "every screen in the registry" sheet that the README
# `halftone_grid.png` showcases.
PANEL_RECIPES: dict[int, list[str]] = {
    1:  ["optimized"],
    2:  ["orig", "recommended"],
    4:  ["orig", "gray", "default_fax", "recommended"],
    6:  ["orig", "gray", "default_fax", "recommended",
         "green-noise", "floyd"],
    8:  ["orig", "gray", "default_fax", "recommended",
         "clustered", "blue-noise", "atkinson", "line"],
    12: ["orig", "gray", "default_fax", "recommended",
         "clustered", "screen-diamond", "ordered", "blue-noise",
         "green-noise", "floyd", "edd", "line"],
    20: ["orig", "gray", "default_fax"] + ALL_HALFTONE_KEYS,
}

# Display labels for content keys. Anything not listed here falls back to a
# Title-cased dither name (so a brand-new screen added to SCREENS automatically
# gets a sensible default label).
_PANEL_LABELS: dict[str, str] = {
    "orig":            "ORIGINAL (color)",
    "gray":            "TRUE GRAYSCALE",
    "default_fax":     "DEFAULT FAX (Otsu)",
    "optimized":       "OPTIMIZED FAX OUTPUT",
    "recommended":     "RECOMMENDED (auto-pick)",
    # Recover-text comparison pair: same options, recover-text forced
    # off / on so the two panels isolate ONLY that pass's effect.
    "no_recover":      "WITHOUT recover-text",
    "with_recover":    "WITH recover-text",
    "screen-square":   "Screen \u2014 square dot",
    "screen-diamond":  "Screen \u2014 diamond dot",
    "screen-ellipse":  "Screen \u2014 ellipse dot",
    "none":            "Hard threshold",
    "green-noise":     "Green noise (AM-FM)",
    "blue-noise":      "Blue noise (FM)",
    "ordered":         "Ordered / Bayer",
    "floyd":           "Floyd-Steinberg",
    "atkinson":        "Atkinson",
    "jarvis":          "Jarvis-Judice-Ninke",
    "stucki":          "Stucki",
    "sierra":          "Sierra",
    "edd":             "Edge-enhancing ED",
    "line":            "Line / woodcut",
    "crosshatch":      "Crosshatch (0\u00b0,90\u00b0)",
    "clustered":       "Clustered-dot (AM)",
    "mezzotint":       "Mezzotint (grain)",
}


def _panel_label(key: str) -> str:
    return _PANEL_LABELS.get(key, key.replace("-", " ").title())


def _resolve_panel_opt(key: str, base_opt: "FaxOptions",
                       recommended_dither: str) -> "FaxOptions | None":
    """Map a panel content key to a FaxOptions instance for rendering.

    Returns None for non-fax reference panels ("orig" / "gray") since those
    are rendered from the source PDF directly without going through the
    bilevel pipeline.
    """
    import dataclasses
    if key in ("orig", "gray"):
        return None
    if key == "default_fax":
        return dataclasses.replace(base_opt, basic=True)
    if key == "optimized":
        return base_opt
    if key == "recommended":
        return dataclasses.replace(base_opt, dither=recommended_dither)
    if key == "no_recover":
        return dataclasses.replace(base_opt, recover_text="off")
    if key == "with_recover":
        return dataclasses.replace(base_opt, recover_text="on")
    if key == "screen-square":
        return dataclasses.replace(base_opt, dither="screen",
                                   dot_shape="square")
    if key == "screen-diamond":
        return dataclasses.replace(base_opt, dither="screen",
                                   dot_shape="diamond")
    if key == "screen-ellipse":
        return dataclasses.replace(base_opt, dither="screen",
                                   dot_shape="ellipse")
    return dataclasses.replace(base_opt, dither=key)


def render_preview(in_pdf: str, page_no: int, out_png: str, opt: FaxOptions):
    """Render exactly the bilevel output for one page as a bare grayscale
    PNG — no labels, no borders, no settings header. This is the underlying
    "bare image" primitive used for pixel-level inspection / overlay; the
    labeled-grid variant lives in `render_contact_sheet`. Keeping this as a
    standalone four-line function deliberately (it's not redundant with the
    labeled-grid path — it has a different output contract)."""
    doc = fitz.open(in_pdf)
    page = doc[page_no - 1]
    img, _ = process_page(page, page_no, opt)
    img.convert("L").save(out_png)
    return out_png


def render_recover_text_preview(in_pdf: str, page_no: int, out_png: str,
                               opt: FaxOptions) -> dict:
    """[LEGACY] Side-by-side proof that recover-text helped: bilevel page
    as it would fax WITHOUT the within-image OCR recolor (left) vs WITH
    it (right). Document-text OCR (outside images) stays on for both
    panels — only the within-image recover-text pass is toggled.

    Now a thin shim over `render_contact_sheet`. New code should call
    `render_contact_sheet(in_pdf, page_no, out_png, opt,
                          include=["no_recover", "with_recover"])`
    directly; this wrapper exists so existing imports keep working.
    Returns the same dict shape as before (with a `recover_text` field
    populated from the WITH panel)."""
    result = render_contact_sheet(
        in_pdf, page_no, out_png, opt,
        include=["no_recover", "with_recover"])
    rt = result.get("panels", {}).get("with_recover", {}).get(
        "recover_text", {}) or {}
    return {"page": page_no, "output": out_png, "recover_text": rt}


def render_sample(in_pdf: str, page_no: int, out_png: str,
                  opt: FaxOptions) -> dict:
    """[LEGACY] Write the default 4-panel sample sheet for one page.

    Now a thin shim over `render_contact_sheet(panel_count=4)`. New code
    should call `render_contact_sheet` directly to pick a panel count.

    Note: the panel CONTENT changed in this revamp — the old 4-panel was
    a "pipeline-stages" view (orig / gray / halftone-only / halftone+
    recover-text), the new 4-panel is a comparison view (orig / gray /
    DEFAULT FAX Otsu / RECOMMENDED auto-pick). The old stages comparison
    is still reachable via `render_contact_sheet(include=["orig", "gray",
    "no_recover", "with_recover"])` (or the legacy `render_recover_text_
    preview` for just the recover-text isolation).
    """
    return render_contact_sheet(in_pdf, page_no, out_png, opt,
                                panel_count=4)


def render_contact_sheet(in_pdf: str, page_no: int, out_png: str,
                         opt: FaxOptions, *,
                         panel_count: int = 4,
                         include: list[str] | None = None,
                         show_header: bool = True) -> dict:
    """Render a one-page-of-PDF contact sheet with N panels for inspection.

    This is the single entry point for the skill's per-page panel comparison
    output. The cell content is selected by `panel_count` (default 4) from
    PANEL_RECIPES — see the comment above PANEL_RECIPES for the per-count
    progression — or fully specified by `include` for power users
    (e.g. `include=["orig", "gray", "clustered", "floyd"]`).

    panel_count must be a key of PANEL_RECIPES (1, 2, 4, 6, 8, 12, or 20).
    The default 4-panel is: ORIGINAL + GRAYSCALE + DEFAULT FAX (Otsu) +
    RECOMMENDED (auto-pick). `--panels 20` ("max") renders every screen in
    the SCREENS registry against the user's own document, equivalent to
    `docs/readme/halftone_grid.png` applied to their input.

    `show_header` adds a 3-line settings header above the grid showing the
    actual run options (preserve_text, ocr_text, recover_text, etc.) plus
    the auto-pick recommendation and reason, so the saved sheet is
    self-documenting weeks later.

    Returns a dict with: page number, output path, per-panel metrics
    (encoded_bytes, est_transmission_s), the auto-recommended dither + reason,
    and the smallest-by-G4-size dither (for cost-conscious users)."""
    import dataclasses

    if include is not None:
        recipe = list(include)
    elif panel_count in PANEL_RECIPES:
        recipe = list(PANEL_RECIPES[panel_count])
    else:
        valid = sorted(PANEL_RECIPES.keys())
        raise ValueError(
            f"panel_count={panel_count} not in {valid} — "
            "pass include=[...] for a custom recipe instead")

    doc = fitz.open(in_pdf)
    page = doc[page_no - 1]
    hdpi, vdpi = RESOLUTIONS[opt.resolution]

    # Reference panels rendered directly from the source PDF.
    color_arr = gray_arr = None
    if "orig" in recipe:
        color_arr = render_page_color(page, hdpi, vdpi, opt.max_scanline_px)
    if "gray" in recipe:
        gray_arr = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)

    # Any fax panel — including "default_fax" — needs eff_dpi + photo_fraction
    # for the per-panel cost estimate and the auto-pick recommendation. Run
    # `_prepare_page` once with the user's opt and reuse the prepared state
    # for every per-dither variation. Avoids paying the OCR cost N times.
    needs_prepared = any(
        k not in ("orig", "gray", "default_fax") for k in recipe)
    needs_anyfax = needs_prepared or "default_fax" in recipe
    prepared = photo_fraction = eff_dpi = None
    photo_features: dict = {}
    rec_dither = rec_reason = None
    if needs_prepared:
        prepared = _prepare_page(page, opt)
        photo_fraction = prepared[2]
        eff_dpi = prepared[9]
        photo_features = prepared[10] or {}
    elif needs_anyfax:
        # Only default_fax (basic) is requested — still need eff_dpi for the
        # cost estimate; photo_fraction stays 0 because basic skips MRC.
        eff_dpi = _page_effective_dpi(page, opt)
        photo_fraction = 0.0
    if needs_anyfax:
        rec_dither, rec_reason = recommend_dither(
            photo_fraction or 0.0, opt.fax_heavy, eff_dpi,
            features=photo_features)

    tmpdir = tempfile.mkdtemp(prefix="faxsheet_")
    panels: list = []
    metrics: dict = {}

    def _add_fax_panel(key: str, bw_img: Image.Image, label: str,
                       dither_name: str | None = None):
        tp = os.path.join(tmpdir, f"{key}.tif")
        nbytes = encode_g4_tiff(bw_img, tp)
        est_s = round(
            nbytes * 8 / opt.line_rate_bps + opt.page_overhead_s, 1)
        panels.append((key, bw_img))
        metrics[key] = {
            "encoded_bytes": nbytes,
            "est_transmission_s": est_s,
            "label": label,
            "dither": dither_name,
        }

    for key in recipe:
        if key == "orig":
            panels.append(("orig", Image.fromarray(color_arr, "RGB")))
            metrics["orig"] = {
                "encoded_bytes": 0, "est_transmission_s": 0.0,
                "original": True, "label": _panel_label("orig"),
                "note": "color source \u00b7 not a fax",
            }
            continue
        if key == "gray":
            panels.append(("gray", Image.fromarray(gray_arr).convert("L")))
            metrics["gray"] = {
                "encoded_bytes": 0, "est_transmission_s": 0.0,
                "original": True, "label": _panel_label("gray"),
                "note": "grayscale source \u00b7 not a fax",
            }
            continue
        if key == "default_fax":
            # Basic mode: grayscale + Otsu, no MRC / OCR / halftone. This
            # is the "naive fax" baseline that the rest of the pipeline
            # improves on, and the visual anchor against which the user
            # decides whether the optimizations are worth the bytes.
            basic_opt = dataclasses.replace(opt, basic=True)
            basic_dpi = _page_effective_dpi(page, basic_opt)
            basic_gray = render_page_gray(
                page, basic_dpi, basic_dpi, basic_opt.max_scanline_px)
            bw = threshold_otsu(basic_gray)
            bw_img = _finalize_basic(bw, basic_opt)
            _add_fax_panel("default_fax", bw_img,
                           _panel_label("default_fax"), dither_name="none")
            continue

        if key in ("no_recover", "with_recover"):
            # The recover-text on/off comparison changes _prepare_page's
            # OCR-driven text masks, so it can't share the cached `prepared`
            # state with the other dither variations — it needs its own
            # full pipeline run. This is the legacy --recover-text-preview
            # behaviour, now folded into the unified path. Capture the
            # PageReport so the caller can pull recover_text statistics
            # (number of words recoloured, polarity decisions) for the
            # legacy `render_recover_text_preview` return shape.
            panel_opt = _resolve_panel_opt(key, opt, rec_dither or "clustered")
            bw_img, rep = process_page(page, page_no, panel_opt)
            _add_fax_panel(key, bw_img, _panel_label(key),
                           dither_name=panel_opt.dither)
            if rep.recover_text:
                metrics[key]["recover_text"] = rep.recover_text
            if rep.ocr_text:
                metrics[key]["ocr_text"] = rep.ocr_text
            continue

        # Everything else is a pipeline panel that reuses the prepared state.
        panel_opt = _resolve_panel_opt(key, opt, rec_dither or "clustered")
        (gray, mask, _pf, _w, _alr, _di, _ii,
         text_black, text_white, _eff, _pf_feat) = prepared
        if key == "optimized":
            d_name = choose_dither(panel_opt.dither, panel_opt.fax_heavy,
                                   photo_fraction, eff_dpi,
                                   features=photo_features)
        elif key == "recommended":
            d_name = rec_dither
        else:
            d_name = panel_opt.dither
        bw = _apply_dither(gray, mask, d_name, panel_opt,
                           eff_dpi, eff_dpi,
                           text_black=text_black, text_white=text_white)
        rep = PageReport(index=page_no)
        rep.dither = d_name
        bw_img = _finalize(bw.astype(np.uint8), rep, panel_opt, protect=mask)
        _add_fax_panel(key, bw_img, _panel_label(key), dither_name=d_name)

    # Pick the recommended panel for the green highlight. Prefer the
    # explicit "recommended" key (auto-pick) when the recipe carries it;
    # otherwise the panel whose dither matches rec_dither; otherwise the
    # smallest-by-G4-size fax panel; otherwise None.
    #
    # A "RECOMMENDED" badge only makes sense on a sheet with multiple fax
    # options to compare; on a single-panel sheet (`--panels 1`) it's
    # noise — the lone panel IS the output by definition. Skip the
    # highlight in that case.
    fax_panels = {k: m for k, m in metrics.items()
                  if not m.get("original")}
    recommended_key = None
    if len(fax_panels) >= 2:
        if "recommended" in metrics:
            recommended_key = "recommended"
        elif rec_dither is not None:
            for k, m in metrics.items():
                if m.get("dither") == rec_dither and not m.get("original"):
                    recommended_key = k
                    break
        if recommended_key is None and fax_panels:
            recommended_key = min(
                fax_panels, key=lambda k: fax_panels[k]["encoded_bytes"])

    header_img = None
    if show_header:
        header_img = _render_settings_header(
            in_pdf, page_no, opt, photo_fraction or 0.0, eff_dpi,
            rec_dither, rec_reason, panel_count=len(panels))

    sheet = _compose_contact_sheet(panels, metrics, recommended_key,
                                   header=header_img)
    sheet.save(out_png)
    return {
        "page": page_no,
        "output": out_png,
        "panel_count": len(panels),
        "panels": metrics,
        "recommended": rec_dither,
        "reason": rec_reason,
        "smallest": (min(
            (k for k, m in metrics.items() if not m.get("original")),
            key=lambda k: metrics[k]["encoded_bytes"], default=None)),
    }


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


def _render_settings_header(in_pdf: str, page_no: int, opt: FaxOptions,
                            photo_fraction: float, eff_dpi: int | None,
                            rec_dither: str | None,
                            rec_reason: str | None,
                            panel_count: int = 4,
                            width: int = 1920) -> Image.Image:
    """Build the 3-line settings strip rendered above the panel grid.

    Self-documents the contact sheet: line 1 says what the source was,
    line 2 lists the layered text/legibility passes the user enabled,
    line 3 lists the channel knobs + the auto-pick recommendation. The
    strip is rendered at `width` and then resized to match the panel
    grid's width by the compositor.
    """
    from PIL import ImageDraw

    pad = 22
    line1_h, line2_h, line3_h = 30, 22, 22
    gap_after_line1 = 10
    H = pad + line1_h + gap_after_line1 + line2_h + 6 + line3_h + pad
    img = Image.new("RGB", (width, H), (244, 244, 244))
    d = ImageDraw.Draw(img)
    d.rectangle([0, H - 2, width - 1, H - 1], fill=(220, 220, 220))

    f1 = _oswald_font(26, "SemiBold")
    f2 = _load_font(15)
    f3 = _load_font(15)

    src = os.path.basename(in_pdf) if in_pdf else "(unknown)"
    res_label = (f"{opt.resolution} ({eff_dpi} DPI)"
                 if eff_dpi else opt.resolution)
    title = (f"{src}  \u00b7  page {page_no}  \u00b7  {res_label}  "
             f"\u00b7  {panel_count} panel{'s' if panel_count != 1 else ''}")
    d.text((pad, pad), title, font=f1, fill=(20, 20, 20))

    # Layered text + legibility passes. `auto` shows the resolved
    # behaviour for ocr_text / recover_text since their default is auto.
    def _onoff(v) -> str:
        if isinstance(v, bool):
            return "on" if v else "off"
        return str(v)
    line2_parts = [
        f"preserve_text={_onoff(opt.preserve_text)}",
        f"ocr_text={_onoff(opt.ocr_text)}",
        f"recover_text={_onoff(opt.recover_text)}",
        f"text_binarize={opt.text_binarize}",
    ]
    if opt.basic:
        line2_parts = ["basic mode (Otsu, no MRC/OCR/halftone)"]
    line2 = "   \u00b7   ".join(line2_parts)
    d.text((pad, pad + line1_h + gap_after_line1), line2,
           font=f2, fill=(60, 60, 60))

    line3_parts = [f"dither={opt.dither}"]
    if opt.fax_heavy:
        line3_parts.append("fax-heavy=on")
    if opt.transmission_safe:
        line3_parts.append("transmission_safe=on (\u22641728 px scanline)")
    if rec_dither and opt.dither == "auto":
        # Surface auto-pick + a SHORT descriptor. The full multi-sentence
        # reason from `recommend_dither()` is too long for the header; pull
        # the first clause (up to the em-dash) out of HALFTONE_INFO so the
        # header reads "auto -> green-noise (Green-noise hybrid AM-FM)".
        short = HALFTONE_INFO.get(rec_dither, "")
        short = short.split(" \u2014 ", 1)[0].strip().rstrip(".,")
        if short:
            line3_parts.append(f"auto\u2192{rec_dither} ({short})")
        else:
            line3_parts.append(f"auto\u2192{rec_dither}")
    if photo_fraction:
        line3_parts.append(f"photo_fraction={photo_fraction:.2f}")
    line3 = "   \u00b7   ".join(line3_parts)
    # Truncate gracefully if line3 still doesn't fit; pop the optional
    # trailing parts (photo_fraction, auto-pick descriptor) before falling
    # back to bare config keys + an ellipsis.
    while line3_parts and f3.getbbox(line3)[2] > width - 2 * pad:
        line3_parts.pop()
        line3 = "   \u00b7   ".join(line3_parts) + " \u2026"
    d.text((pad, pad + line1_h + gap_after_line1 + line2_h + 6), line3,
           font=f3, fill=(60, 60, 60))
    return img


_WRAP_BREAK_CHARS = ";:,."           # word-final clause punctuation
_WRAP_BREAK_TOKENS = {"\u00b7",      # middle-dot, em-dash, en-dash, pipe
                      "\u2014", "\u2013", "|"}


def _wrap_text(text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
    """Word-wrap with CLAUSE-AWARE break preference.

    Standard greedy wrap finds the rightmost fit per line, which produces
    visually ugly orphans when a long string almost fits — e.g.
    `…recover-text OFF · fax | preview` orphans the trailing `preview`.

    This wrapper does greedy first, then on every line EXCEPT the final
    one scans backward for a "preferred" break right after end-of-clause
    punctuation (`;:,.·`) or a standalone separator token (`·`, em/en
    dash, pipe). If a preferred break exists *and* taking it would still
    leave at least 50 % of `max_w` filled, it wins. The result is wraps
    that land on natural clause boundaries instead of in the middle of a
    thought. Final-line overflow is truncated with a horizontal ellipsis.
    """
    words = text.split()
    if not words:
        return [""]

    def line_w(seg):
        return font.getbbox(" ".join(seg))[2]

    def ends_clause(w):
        """End of a clause: a word terminating in `;:,.·`, possibly with a
        trailing closing bracket (`#808080);`, `[foo],`, etc.)."""
        if not w:
            return False
        if w[-1] in _WRAP_BREAK_CHARS:
            return True
        if len(w) >= 2 and w[-1] in ")]}\u00bb" and w[-2] in _WRAP_BREAK_CHARS:
            return True
        return False

    def is_separator(w):
        return w in _WRAP_BREAK_TOKENS

    lines: list[str] = []
    i = 0
    while i < len(words) and len(lines) < max_lines:
        # Greedy: rightmost word that still fits.
        best_j = i
        while best_j < len(words) and line_w(words[i:best_j + 1]) <= max_w:
            best_j += 1
        best_j -= 1
        if best_j < i:                              # single overflow word
            best_j = i
        is_last_line = len(lines) == max_lines - 1
        if not is_last_line:
            # Phase 1: prefer a clause-end break (word ending in `;:,.·`).
            # We scan strictly backward so the earliest clause break that
            # still keeps the line ≥ 40 % full wins — beats the stranded
            # `·` case where greedy stops on a standalone separator.
            preferred = None
            for k in range(best_j, i - 1, -1):
                if ends_clause(words[k]):
                    preferred = k
                    break
            # Phase 2: fall back to a standalone separator break (e.g. `·`)
            # only if it's NOT at the very end of the greedy line — we
            # don't want to strand a `·` at the right edge.
            if preferred is None:
                for k in range(best_j - 1, i - 1, -1):
                    if is_separator(words[k]):
                        preferred = k
                        break
            if (preferred is not None and preferred != best_j and
                    line_w(words[i:preferred + 1]) >= max_w * 0.4):
                best_j = preferred
            elif is_separator(words[best_j]) and best_j > i:
                # Greedy ended on a stranded separator — push it to the
                # next line so the break lands on the previous word.
                best_j -= 1
        lines.append(" ".join(words[i:best_j + 1]))
        i = best_j + 1

    if i < len(words) and lines:                   # ran out of lines
        ell = "\u2026"
        tail = lines[-1]
        while tail and font.getbbox(tail + ell)[2] > max_w:
            tail = tail[:-1].rstrip()
        lines[-1] = (tail + ell) if tail else ell
    return lines


def _compose_contact_sheet(panels, metrics, recommended, cell_w=480,
                           header: "Image.Image | None" = None):
    """panels: list of (method_name, PIL '1' image). Returns an RGB contact
    sheet with a labeled, metric-annotated panel per method.

    If `header` is provided, it is composited as a strip ABOVE the panel
    grid (full canvas width; height taken from the supplied image). Use
    `_render_settings_header()` to build a standard run-options strip."""
    from PIL import ImageDraw
    rendered = []
    for name, img in panels:
        rgb = img.convert("RGB")          # keep color references in color
        ch = max(1, int(rgb.height * (cell_w / rgb.width)))
        rendered.append((name, rgb.resize((cell_w, ch), Image.LANCZOS)))

    # Header layout. `inset` is a uniform horizontal padding for label +
    # note; `label_top` and `note_top` are the y-offsets within the band.
    # The vertical rhythm:
    #   - generous gap between heading (22 pt SemiBold) and subheading so
    #     the subheading reads as a *separate* line of metadata, not a
    #     stuck-on second line of the label;
    #   - tight line-leading inside the subheading (≈1.27× the 15 pt
    #     size) so wrapped notes look like one continuous thought instead
    #     of two disconnected sentences with a big gap between them.
    inset = 16
    label_top = 14
    note_top = 41                              # 27 px after label_top (was 42; -35%)
    note_line_h = 19                           # tight subheading leading
    cap = note_top + 2 * note_line_h + 6       # 85 px header band
    pad, title_h = 18, 18
    # Grid layout: up to 4 panels in a single row; 5–6 panels go to 3 cols
    # (matches the original `--compare-page` 2×3); 7+ panels (the 8/12/20
    # contact sheets) need a 4-col layout so the rendered cells don't get
    # gigantic and aspect-correct images still fit.
    n = len(rendered)
    if n <= 4:
        cols = max(1, n)
    elif n <= 6:
        cols = 3
    else:
        cols = 4
    rows = (n + cols - 1) // cols
    cell_h = max(r.height for _, r in rendered)
    W = pad + cols * (cell_w + pad)
    # Settings header strip height. The strip is centred within the W-wide
    # canvas; if it was rendered at a different width we resize it down.
    header_resized = None
    header_h = 0
    if header is not None:
        if header.width != W:
            header_h = max(1, int(round(header.height * W / header.width)))
            header_resized = header.resize((W, header_h), Image.LANCZOS)
        else:
            header_resized = header
            header_h = header.height
        header_h += pad         # gap between header strip and first row
    H = header_h + title_h + rows * (cell_h + cap + pad) + pad
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    if header_resized is not None:
        canvas.paste(header_resized, (0, 0))
    d = ImageDraw.Draw(canvas)
    lf, sf = _oswald_font(22, "SemiBold"), _load_font(15)
    note_max_w = cell_w - 2 * inset

    for i, (name, rgb) in enumerate(rendered):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = header_h + title_h + r * (cell_h + cap + pad)
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
        # Cell heading: prefer the per-panel `label` from metrics (it's
        # already formatted — e.g. "DEFAULT FAX (Otsu)", "Green noise (AM-FM)"),
        # and only fall back to an uppercased panel-key when no label is set.
        # The ">> RECOMMENDED <<" suffix is appended only when the cell isn't
        # ALREADY a "RECOMMENDED (auto-pick)" label, otherwise it doubles up.
        base_label = m.get("label") or name.upper()
        if is_rec and "RECOMMENDED" not in base_label.upper():
            label_text = f"{base_label}   >> RECOMMENDED <<"
        else:
            label_text = base_label
        # Label rarely needs wrapping but keep the guard so an unusually long
        # method name (e.g. "FLOYD-STEINBERG   >> RECOMMENDED <<") doesn't
        # bleed into the next cell either.
        label_lines = _wrap_text(label_text, lf, note_max_w, max_lines=1)
        d.text((x + inset, y + label_top), label_lines[0],
               font=lf, fill=(10, 10, 10))
        note_text = (m.get("note", "source \u00b7 not a fax") if is_orig else
                     (f"{m.get('encoded_bytes', 0) / 1024:.0f} KB  \u00b7  "
                      f"~{m.get('est_transmission_s', 0):.0f}s / page"))
        note_lines = _wrap_text(note_text, sf, note_max_w, max_lines=2)
        for li, line in enumerate(note_lines):
            d.text((x + inset, y + note_top + li * note_line_h), line,
                   font=sf, fill=(36, 36, 36))
        canvas.paste(rgb, (x, y + cap))
        d.rectangle([x, y, x + cell_w, y + cap + rgb.height],
                    outline=(38, 148, 38) if is_rec else (178, 178, 178),
                    width=3 if is_rec else 1)
    return canvas


def render_comparison(in_pdf: str, page_no: int, out_png: str, opt: FaxOptions,
                      methods=None, include_original: bool = False) -> dict:
    """[LEGACY] Render one page through several halftone methods into a
    single contact sheet so a human can pick by eye ("eye tokens").

    Now a thin shim over `render_contact_sheet`. New code should call
    `render_contact_sheet(in_pdf, page_no, out_png, opt,
                          include=["orig","gray", ...halftones...])`
    directly; this wrapper exists so existing imports keep working.

    Maps the legacy args to the unified call:
      - `methods` default → curated 6-up (`COMPARE_METHODS`) when no
        references, or 4-up (`COMPARE4_METHODS`) when references are
        included, matching the old behaviour
      - `include_original=True` prepends "orig" + "gray" to the recipe

    Returns the same dict shape as before so legacy callers keep
    working (`page`, `methods`, `recommended`, `reason`, `smallest`,
    `output`).
    """
    if methods is None:
        methods = COMPARE4_METHODS if include_original else COMPARE_METHODS
    include = (["orig", "gray"] if include_original else []) + list(methods)
    result = render_contact_sheet(in_pdf, page_no, out_png, opt,
                                  include=include)
    # Build the legacy return shape: filter out the reference panels and
    # surface the per-method G4 stats under "methods" (not "panels"), and
    # carry the auto-pick recommendation through.
    methods_metrics = {k: v for k, v in result.get("panels", {}).items()
                       if not v.get("original")}
    smallest = result.get("smallest")
    rec = result.get("recommended")
    if rec not in methods_metrics:
        # Auto-pick wasn't in the user's method list — fall back to
        # smallest, matching the historical behaviour exactly.
        rec = smallest
    return {
        "page": page_no,
        "methods": methods_metrics,
        "recommended": rec,
        "reason": result.get("reason"),
        "smallest": smallest,
        "output": out_png,
    }
