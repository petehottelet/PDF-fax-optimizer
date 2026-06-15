# General (Size) Optimization Reference

For `--mode size`. Goal: smallest file that still reads as a faithful color/gray
PDF. Unlike fax mode, the output stays a normal multi-tone document.

## What actually drives PDF size

In rough order, for most real-world PDFs:

1. **Raster images at higher resolution than needed.** A 3000-dpi phone photo
   placed at 2 inches wide carries ~6× the pixels any screen or printer uses.
   Downsampling is almost always the biggest single win.
2. **Image encoding.** Photographs → JPEG; screenshots/line art/flat color →
   PNG/Flate or JBIG2/CCITT if bilevel. Using the wrong codec (lossless on a
   photo, or lossy on a screenshot) wastes bytes or wrecks quality.
3. **Uncompressed or poorly compressed content streams** and object structure —
   recovered losslessly by qpdf.
4. **Embedded full fonts** that could be subsetted.
5. **Redundant/duplicate objects, unused resources, metadata bloat.**

## Procedure

```bash
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf --mode size \
    --target-dpi 150 --jpeg-quality 75 --linearize
```

Steps the script performs:

1. **Inventory images** (resolution-in-context, colorspace, current codec).
2. **Downsample** any image whose effective dpi exceeds `--target-dpi`
   (150 for screen/email; 200–300 if it may be printed).
3. **Re-encode** photos as JPEG at `--jpeg-quality`; keep lossless codecs for
   line art / flat-color / already-bilevel images.
4. **qpdf pass** for lossless object-stream compression and cleanup.
5. **Linearize** (`--linearize`) for fast web view ("Fast Web View").
6. **Report** before/after bytes and per-image savings.

## Choosing `--target-dpi` and `--jpeg-quality`

| Destination | target-dpi | jpeg-quality |
|---|---|---|
| Email / on-screen reading | 110–150 | 70–80 |
| General-purpose / mild print | 200 | 80–85 |
| Print-quality archive | 300 | 88–92 |

Below ~70 quality, JPEG artifacts become visible on text-in-images and
gradients. Above ~92 you pay bytes for invisible gains.

## When NOT to recompress (leave it alone)

Recompression can *increase* size or *degrade* quality. Skip / be conservative when:

- **Text-only or vector-heavy PDFs.** There's little raster to shrink;
  recompressing risks rasterizing crisp vectors. Use `--mode size-lossless`
  (qpdf only) instead.
- **Images already at/below target dpi.** Re-encoding a small JPEG just adds
  generational artifacts. The script skips these by default.
- **Already-optimized / linearized PDFs.** Check first; a second lossy pass only
  hurts.
- **Documents headed to print** — don't downsample to 150 dpi if they'll be
  printed at A3.

## Lossless path

```bash
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf --mode size-lossless
```

Runs qpdf's structural/stream optimization and linearization only — no image
downsampling or re-encoding. Use when the user wants a smaller file with
*zero* quality change, or for vector/text-heavy PDFs.

## Verifying the result

- Confirm the output still opens and renders (the script does a load check).
- Spot-check the heaviest pages visually if quality matters.
- If savings are negligible (<5%), tell the user the PDF was already lean and
  recommend the lossless path rather than pushing quality down.
