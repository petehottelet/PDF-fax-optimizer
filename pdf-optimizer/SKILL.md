---
name: pdf-optimizer
description: >-
  Maximize document quality and readability for a delivery channel — above all,
  make a PDF arrive LEGIBLE over a noisy fax network (a fax's whole job is to be
  read), or shrink one for email/web. Use to make a PDF faxable, fix a fax that
  comes out muddy, garbled, or unreadable, keep small text and signatures
  readable over fax, halftone photos for fax, compress a PDF, or hit a target
  file size. Triggers: "prep these scans to fax", "why is my fax unreadable".
compatibility: >-
  Requires Python 3 with PyMuPDF (fitz), Pillow, numpy, opencv-python, and
  img2pdf, plus the qpdf CLI. Ghostscript is optional. Run
  scripts/check_deps.py first; it installs the pip packages if missing.
---

# PDF Optimizer

This skill optimizes PDFs for a **delivery channel**, not in the abstract. The
single most important question is *where is this PDF going?* — because the right
trade-offs for "email it" are the opposite of the right trade-offs for "fax it."

**For fax, legibility is the entire objective.** A fax exists to be *read on the
other end*, often after surviving a low-resolution, 1-bit, lossy-by-design
Group-3 transmission over a noisy phone line. So in fax mode this skill is built
to **maximize document quality and enhance reception/readability** — keep text
crisp, small fonts and signatures intact, and photos recognizable — *first*.
Smaller files and faster transmission are welcome side effects, never the goal:
a tiny fax that arrives unreadable is a failure. When a trade-off is forced,
choose what stays readable on the receiving machine.

## Step 0 — Pick the mode

| Signal from the user | Mode | What "good" means |
|---|---|---|
| "too big to email", "shrink", "compress", "target N MB", "web view" | **size** | Smallest file that stays visually faithful, in color/grayscale |
| "fax", "faxable", "bilevel", "black & white for the fax", "comes out garbled/muddy", "send to the clinic's fax" | **fax** | A 1-bit, fax-native PDF (or TIFF) that **arrives readable** on the receiving machine — maximum legibility first; small size and speed are secondary |

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

The goal of every choice here is **a document that arrives readable.** Each knob
exists to protect legibility against a lossy channel — keeping edges sharp,
rescuing faint strokes, and choosing halftones that don't collapse to mud after
the receiving machine re-thresholds. Optimize for what the recipient can *read*,
not for the smallest byte count.

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
   picks per the fidelity/compression trade-off; override with any of the
   methods in *Halftone methods* below.
5. **Defend legibility:** optionally thicken hairline strokes and sub-minimum
   fonts that would vanish at low DPI; warn on inverted (white-on-black) regions
   that balloon transmission time.
6. **Emit CCITT G4** inside the PDF (default) or as a Class-F multipage TIFF
   (`--format tiff`) for a fax-ready file — losslessly, via img2pdf, never
   re-encoded.
7. **Report** estimated transmission seconds per page (from the actual G4-encoded
   size), total pages, and any legibility/inversion warnings, so the result is
   inspectable *before* someone faxes something unreadable.

### Halftone methods (the top 5)

A continuous-tone photo can't exist in 1-bit fax; it must be *simulated* with
patterns of black dots. The method chosen is the single biggest lever on how a
photo reads after transmission, and each sits at a different point on the
**fidelity ↔ transition-density** curve (transitions = compression cost = line
fragility). The skill ships five, spanning the design space (full theory in
`references/fax-optimization.md`):

1. **`clustered`** — clustered-dot AM screening (newsprint). Dots grow in
   clusters → long runs → **best G4 compression and most robust over a noisy
   line**; lowest apparent resolution. The default for `--fax-heavy`.
2. **`blue-noise`** — void-and-cluster FM screening. Isotropic, organic stipple
   with **no directional "worms"**; excellent perceived detail, middling
   compression.
3. **`atkinson`** — Atkinson error diffusion. Clean whites and crisp thin
   features; good detail, looser compression than screening.
4. **`floyd`** — Floyd–Steinberg error diffusion. The classic; **maximum
   detail**, but its dispersed speckle is the **worst case for G4 size** and the
   most fragile over a bad line.
5. **`ordered`** — Bayer ordered dithering. Fast and predictable crosshatch;
   middling on both detail and compression.

(Also selectable: `jarvis`, `stucki`, `sierra` heavier error-diffusion kernels,
and `none` = hard threshold for pure text / line art / barcodes.) Always match
the screen to the fax resolution, or the halftone collapses to mud when the
receiver re-thresholds — the pipeline scales the clustered cell from the dpi.

### Maximally productive preview — let the user spend their *eye tokens*

Algorithms can rank compression objectively, but **only a human eye can judge
"does this read?"** So don't just pick silently — generate a side-by-side
**comparison contact sheet** and let the user spend their *eye tokens* on the
real, encoded output:

```bash
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf --mode fax \
    --fax-resolution fine --compare-page 1 --report OUTPUT.report.json
```

`--compare-page N` renders that page through all five halftone methods into one
labeled PNG (`OUTPUT.compare_pN.png`), each panel annotated with its **actual
G4 size and estimated transmission time**, and the **OPTIMAL pick highlighted**.
The skill therefore does both jobs the user asked for: (a) it **suggests the
optimal** method from the page's content (photo fraction, fax-heavy, line
condition), and (b) it lets the user **choose the optimal** by eye from the
contact sheet. Offer this whenever a fax has meaningful photo content — then
re-run with the chosen `--dither` for the final file. (Use `--compare-methods`
to override which methods appear.)

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
flagged. For fax jobs, **legibility is the acceptance test**:

- For a single rendering, offer a **preview** of the actual bilevel output
  (`--preview-page N` writes a PNG of exactly what will be transmitted).
- When there's real photo content, offer the **comparison contact sheet**
  (`--compare-page N`) so the user can spend their *eye tokens* and pick the
  halftone that reads best — the skill highlights its recommended/optimal pick,
  but the human makes the call.

If anything is borderline — small text, faint signatures, muddy photos —
recommend the knob that recovers it (`--thicken`, a higher `--fax-resolution`,
or a different `--dither`) rather than shipping an unreadable fax.

## Reference files

- `references/fax-optimization.md` — the fax constraint model, resolutions,
  dithering theory, MRC segmentation, legibility defense, transmission
  economics. **Read before running fax mode.**
- `references/general-optimization.md` — size-reduction procedure, when to
  recompress vs. leave alone, lossless path.
- `references/config-schema.md` — full JSON config schema + annotated example.
