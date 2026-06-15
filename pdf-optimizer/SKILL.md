---
name: pdf-optimizer
description: >-
  Optimize a PDF for a delivery channel: shrink it for email/web, or convert it
  to a fax-ready 1-bit bilevel file. Use when someone wants to compress a PDF,
  hit a target file size, make a PDF faxable or black-and-white for the fax, fix
  a document that looks muddy or garbled when faxed, halftone photos for fax,
  downsample images, or linearize for fast web view. Triggers include "too big
  to email", "prep these scans to fax", and "why does my fax come out garbled".
compatibility: >-
  Requires Python 3 with PyMuPDF (fitz), Pillow, numpy, opencv-python, and
  img2pdf, plus the qpdf CLI. Ghostscript is optional. Run
  scripts/check_deps.py first; it installs the pip packages if missing.
---

# PDF Optimizer

This skill optimizes PDFs for a **delivery channel**, not in the abstract. The
single most important question is *where is this PDF going?* — because the right
trade-offs for "email it" are the opposite of the right trade-offs for "fax it."

## Step 0 — Pick the mode

| Signal from the user | Mode | What "good" means |
|---|---|---|
| "too big to email", "shrink", "compress", "target N MB", "web view" | **size** | Smallest file that stays visually faithful, in color/grayscale |
| "fax", "faxable", "bilevel", "black & white for the fax", "comes out garbled/muddy", "send to the clinic's fax" | **fax** | A 1-bit, fax-native PDF (or TIFF) that survives Group-3 transmission *and stays legible* |

If genuinely ambiguous, ask one question. Don't guess between size and fax —
they produce very different artifacts.

Then verify the environment once:

```bash
python3 scripts/check_deps.py        # installs missing pip deps, reports qpdf/gs
```

## Mode: size

Goal: reduce bytes while the document still reads as a normal color/gray PDF.
Full procedure and tuning knobs are in `references/general-optimization.md`.
The short version:

```bash
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf --mode size \
    --target-dpi 150 --jpeg-quality 75 --linearize
```

The script downsamples over-resolution images, re-encodes them, runs qpdf for
lossless structural/stream optimization and linearization, and reports the
before/after size. Read `references/general-optimization.md` before changing
defaults — especially the guidance on *when not to recompress* (already-small
images, vector-heavy or text-only PDFs, and the lossless `--mode size-lossless`
path that only runs qpdf).

## Mode: fax

This is the substantive part of the skill, and the part most tools get wrong.
**Read `references/fax-optimization.md` before running fax mode** — it explains
the constraints below and why each knob exists. Do not skip it; the defaults
only make sense once you understand the Group-3 transmission model.

The core constraint: Group-3 fax is **1-bit bilevel** (pure black/white, no
gray) at fixed, *anisotropic* resolutions, compressed with run-length codes
(CCITT G3/G4) *along each scanline*. That last fact is the hinge for every
decision — anything that creates many black↔white transitions per line
compresses badly, transmits slowly, and is more vulnerable to line noise. Fax
mode is fundamentally a balance between **visual fidelity** and **transition
density**.

Run it:

```bash
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf --mode fax \
    --fax-resolution fine --dither auto --report OUTPUT.report.json
```

What the pipeline does, per page (details in the reference):

1. **Rasterize at a fax-native resolution**, honoring the anisotropic DPI
   (`standard` 204×98, `fine` 204×196, `superfine` 204×391) and clamping the
   scanline to 1728 px. Square resampling distorts the page — the pipeline
   resamples horizontal and vertical axes independently.
2. **Segment content (MRC-lite).** Text and line art are routed to a hard
   threshold and kept crisp; photos / continuous-tone regions are routed to a
   halftone. *Never dither text* — it destroys edge sharpness and legibility.
   Region detection uses the PDF's own embedded-image rectangles, not guesswork.
3. **Pre-clean:** flatten near-white backgrounds to pure white (so faint content
   survives thresholding), despeckle isolated black pixels, and deskew.
4. **Halftone the photo regions** with the chosen algorithm. `--dither auto`
   picks per the fidelity/compression trade-off; override with
   `clustered` (best compression, survives noisy lines), `floyd`/`atkinson`
   (best detail, worst compression), or `ordered`.
5. **Defend legibility:** optionally thicken hairline strokes and sub-minimum
   fonts that would vanish at low DPI; warn on inverted (white-on-black) regions
   that balloon transmission time.
6. **Emit CCITT G4** inside the PDF (default) or as a Class-F multipage TIFF
   (`--format tiff`) for a fax-ready file — losslessly, via img2pdf, never
   re-encoded.
7. **Report** estimated transmission seconds per page (from the actual G4-encoded
   size), total pages, and any legibility/inversion warnings, so the result is
   inspectable *before* someone faxes something unreadable.

### Choosing the dither (the non-obvious call)

`references/fax-optimization.md` covers this in full, but the headline:

- **Clustered-dot / ordered** halftone compresses far better and survives a
  noisy line — at the cost of apparent resolution. Default for `--fax-heavy`.
- **Error diffusion** (Floyd-Steinberg, Atkinson) gives the best perceived
  photo detail but produces dispersed high-frequency speckle — the *worst case*
  for run-length compression and the most fragile over a bad line.
- Match the screen frequency to the fax resolution, or the halftone collapses to
  mud when the receiving machine re-thresholds.

### Special content checks

Always run the report and skim the warnings. Watch for: **barcodes / QR codes**
(verify they survive bilevel — they want a clean threshold, *not* dither),
**signatures / handwriting** (thin low-contrast strokes — candidates for
thickening), **already-bilevel input** (detect and skip reprocessing), and
**large black / inverted areas** (slow and error-prone to transmit).

## Config file (optional)

Instead of flags, pass `--config config.json`. Schema and an annotated example:
`references/config-schema.md`. Flags override config values.

## Output

Always finish by telling the user the output path(s), the before/after size (or
for fax, total pages + estimated transmission time), and any warnings the report
flagged. For fax jobs, offer to render a **preview** of the actual bilevel
output (`--preview-page N` writes a PNG of exactly what will be transmitted) so
they can eyeball legibility before sending.

## Reference files

- `references/fax-optimization.md` — the fax constraint model, resolutions,
  dithering theory, MRC segmentation, legibility defense, transmission
  economics. **Read before running fax mode.**
- `references/general-optimization.md` — size-reduction procedure, when to
  recompress vs. leave alone, lossless path.
- `references/config-schema.md` — full JSON config schema + annotated example.
