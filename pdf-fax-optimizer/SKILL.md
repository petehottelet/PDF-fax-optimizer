---
name: pdf-fax-optimizer
description: >-
  Maximize document quality and readability when faxing a PDF, Word, PowerPoint,
  Excel, or image file — make it arrive LEGIBLE after a noisy 1-bit Group-3
  transmission (a fax's whole job is to be read), then optionally transmit it via
  a cloud fax API. Use to make a document faxable, convert it to bilevel, fix a
  muddy or unreadable fax, keep small text and signatures readable, halftone
  photos, or send a fax. Triggers: "fax this Word doc", "prep these scans to
  fax", "send this as a fax".
compatibility: >-
  Requires Python 3 with PyMuPDF (fitz), Pillow, numpy, opencv-python, and
  img2pdf. Sending a fax additionally needs the requests package; faxing
  Office/OpenDocument files (Word/PowerPoint/Excel) needs LibreOffice (headless).
  No CLI tools are required for PDF/image input. Run scripts/check_deps.py first;
  it installs the pip packages if missing.
---

# PDF FAX

This skill converts a PDF into a **fax-ready** file and, above all, one that
**arrives LEGIBLE** on the receiving machine.

**Legibility is the entire objective.** A fax exists to be *read on the other
end*, often after surviving a low-resolution, 1-bit, lossy-by-design Group-3
transmission over a noisy phone line. So this skill is built to **maximize
document quality and enhance reception/readability** — keep text crisp, small
fonts and signatures intact, and photos recognizable — *first*. Smaller files
and faster transmission are welcome side effects, never the goal: a tiny fax
that arrives unreadable is a failure. When a trade-off is forced, choose what
stays readable on the receiving machine.

> **Just need to shrink a PDF for email or the web?** That's a different job
> with the opposite trade-offs — use the companion skill instead:
> [pdf-email-optimizer](https://github.com/petehottelet/pdf-email-optimizer).

Verify the environment once:

```bash
python3 scripts/check_deps.py        # installs missing pip deps
```

## How it works

**Read `references/fax-optimization.md` before running** — it explains the
constraints below and why each knob exists. Do not skip it; the defaults only
make sense once you understand the Group-3 transmission model.

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
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf \
    --fax-resolution fine --dither auto --report OUTPUT.report.json
```

### Input: PDF, Office, or image

`INPUT` doesn't have to be a PDF. The optimizer normalizes other formats to PDF
first (via `scripts/to_pdf.py`), then runs the identical pipeline:

- **PDF** — used directly.
- **Images** (`.png/.jpg/.tif/.bmp/.gif/.webp`) — wrapped to PDF with `img2pdf`
  (no extra tools).
- **Office / OpenDocument / text** (`.doc/.docx/.rtf/.odt/.txt`, `.ppt/.pptx/.odp`,
  `.xls/.xlsx/.ods/.csv`) — rendered to PDF by **LibreOffice headless**
  (`soffice`). If LibreOffice isn't installed the skill says so; install it (no
  GUI needed) or have the user export to PDF. `--keep-converted-pdf` keeps the
  intermediate PDF beside the output.

```bash
python3 scripts/optimize_pdf.py proposal.docx -o proposal.fax.pdf \
    --fax-resolution fine --dither auto          # Word in, faxable PDF out
```

What the pipeline does, per page (details in the reference):

1. **Rasterize at a fax-native resolution**, honoring the anisotropic DPI
   (`standard` 204×98, `fine` 204×196, `superfine` 204×391) and clamping the
   scanline to 1728 px. Square resampling distorts the page — the pipeline
   resamples horizontal and vertical axes independently.
2. **Segment content (MRC-lite).** Text and line art are routed to an *adaptive
   binarizer* and kept crisp; photos / continuous-tone regions are routed to a
   halftone. *Never dither text* — it destroys edge sharpness and legibility.
   Region detection uses the PDF's own embedded-image rectangles, not guesswork.
   **Text baked into an image** (captions, signs, screenshots, or a whole page
   scanned as one image) is detected *inside* the photo region and kept legible
   instead of being halftoned (`--no-text-in-image` to disable).
3. **Pre-clean:** flatten near-white backgrounds to pure white (so faint content
   survives thresholding), despeckle isolated black pixels, and deskew.
4. **Binarize the text/line content** with `--text-binarize` (default `contrast`,
   tuned for legibility: it pulls gray / light-gray text on white to *solid black*
   instead of dropping it, and holds up over dark header bars, reverse type, and
   uneven illumination far better than a single global cut; `sauvola`/`niblack`/
   `wolf`/`bradley`/`otsu` also available). Text is never halftoned.
5. **Halftone the photo regions** with the chosen schema. Photos first get
   *dot-gain pre-correction* (`--tone-curve auto`, so midtones don't plug to a
   black silhouette) and optional edge sharpening (`--sharpen`). `--dither auto`
   picks per the fidelity/compression trade-off; override with any of the
   schemas in *Halftone schemas* below.
6. **Defend legibility:** optionally thicken hairline strokes and sub-minimum
   fonts that would vanish at low DPI; warn on inverted (white-on-black) regions
   that balloon transmission time.
7. **Emit CCITT G4** inside the PDF (default) or as a Class-F multipage TIFF
   (`--format tiff`) for a fax-ready file — losslessly, via img2pdf, never
   re-encoded.
8. **Report** estimated transmission seconds per page (from the actual G4-encoded
   size), total pages, and any legibility/inversion warnings, so the result is
   inspectable *before* someone faxes something unreadable.

The mindset is **optimize the document for the channel**, not "make it look like
a fax": keep text vector-crisp, render photos so they stay recognizable, and
spend bits where they buy legibility. The halftone/binarization *schema* is the
control surface for that — pick it deliberately.

### Halftone schemas (the curated 6-up)

A continuous-tone photo can't exist in 1-bit fax; it must be *simulated* with
patterns of black dots. The schema chosen is the single biggest lever on how a
photo reads after transmission, and each sits at a different point on the
**fidelity ↔ transition-density** curve (transitions = compression cost = line
fragility). The skill ships six front-line schemas spanning the design space
(full theory in `references/fax-optimization.md`):

1. **`clustered`** — clustered-dot AM screening (newsprint). Dots grow in
   clusters → long runs → **best G4 compression and most robust over a noisy
   line**; lowest apparent resolution. The default for `--fax-heavy`.
2. **`green-noise`** — hybrid AM–FM screening. Mid-size dot clusters give
   **blue-noise detail with clustered-dot run-length and robustness**; tune with
   `--green-noise-coarseness` (~2 detail … 8 robust). The `auto` pick for a
   moderate photo that must survive a real line.
3. **`blue-noise`** — void-and-cluster FM screening. Isotropic, organic stipple
   with **no directional "worms"**; excellent perceived detail, middling
   compression.
4. **`atkinson`** — Atkinson error diffusion. Clean whites and crisp thin
   features; good detail, looser compression than screening.
5. **`floyd`** — Floyd–Steinberg error diffusion. The classic; **maximum
   detail**, but its dispersed speckle is the **worst case for G4 size** and the
   most fragile over a bad line.
6. **`line`** (aliases `woodcut`/`engraving`) — horizontal line screen. Tone
   becomes horizontal stripes that **thicken with darkness**; because the strokes
   run *along the scanline* it produces almost entirely long runs → **G4 size and
   robustness on par with `clustered`**, while reading as a crisp, high-contrast
   engraving rather than mud. Great when a line is bad and the photo just has to
   stay recognizable.

(Also selectable: `ordered` Bayer; `edd` edge-enhancing error diffusion for text
over a photographic background; `jarvis`/`stucki`/`sierra` heavier kernels; and
`none` = hard threshold for pure text / line art / barcodes.) All screen schemas
are **anisotropically tuned** from the fax DPI so dots stay round on paper and
the screen doesn't collapse to mud when the receiver re-thresholds.

### Maximally productive preview — let the user spend their *eye tokens*

Algorithms can rank compression objectively, but **only a human eye can judge
"does this read?"** So don't just pick silently — generate a side-by-side
**comparison contact sheet** and let the user spend their *eye tokens* on the
real, encoded output:

```bash
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf \
    --fax-resolution fine --compare-page 1 --report OUTPUT.report.json
```

`--compare-page N` renders that page through the curated 6-up of halftone methods
into one labeled PNG (`OUTPUT.compare_pN.png`), each panel annotated with its
**actual G4 size and estimated transmission time**, and the **OPTIMAL pick
highlighted**.
The skill therefore does both jobs the user asked for: (a) it **suggests the
optimal** method from the page's content (photo fraction, fax-heavy, line
condition), and (b) it lets the user **choose the optimal** by eye from the
contact sheet. Add `--compare-original` to lead the sheet with two reference
panels — the original in color (#1) and a true grayscale of it (#2) — followed
by four halftones, so the viewer can see exactly what each schema is
approximating. Offer this whenever a fax has meaningful photo content — then
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

Always finish by telling the user the output path(s), the total pages +
estimated transmission time, and any warnings the report flagged. **Legibility
is the acceptance test**:

- For a single rendering, offer a **preview** of the actual bilevel output
  (`--preview-page N` writes a PNG of exactly what will be transmitted).
- When there's real photo content, offer the **comparison contact sheet**
  (`--compare-page N`) so the user can spend their *eye tokens* and pick the
  halftone that reads best — the skill highlights its recommended/optimal pick,
  but the human makes the call.

If anything is borderline — small text, faint signatures, muddy photos —
recommend the knob that recovers it (`--thicken`, a higher `--fax-resolution`,
or a different `--dither`) rather than shipping an unreadable fax.

## Send it (optional)

The skill can also **transmit** the optimized file through a cloud fax API — no
machine, modem, or phone line, just an API key and the recipient number in
**E.164** (e.g. `+14155551234`). Built-in providers: `mfax` (mFax/Documo, the
default), `phaxio` (Phaxio/Sinch), and `generic` (any upload API — Telnyx, SRFax,
etc.). Full details and per-provider request shapes: `references/sending.md`.

Optimize and send in one step (always pass keys via env, and **`--dry-run` first**
to confirm the request before transmitting):

```bash
export MFAX_API_KEY=sk_live_xxx
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.fax.pdf \
    --fax-resolution fine --dither auto \
    --send mfax --to +14155551234 --dry-run     # drop --dry-run to transmit
```

Or send an already-optimized file directly:

```bash
python3 scripts/send_fax.py OUTPUT.fax.pdf --provider mfax --to +14155551234
```

Rules: **never** put an API key on the command line (use the provider's env var);
confirm the recipient number with the user before a real send; show the user the
`--dry-run` request and the legibility check (preview/report) before transmitting;
and remember a submit response means *queued*, not *delivered* — point the user
to the provider's status webhook/endpoint to confirm receipt.

## Reference files

- `references/fax-optimization.md` — the fax constraint model, resolutions,
  dithering theory, MRC segmentation, legibility defense, transmission
  economics. **Read before running.**
- `references/config-schema.md` — full JSON config schema + annotated example.
- `references/sending.md` — transmit via a cloud fax API (mFax, Phaxio,
  generic): endpoints, auth, env vars, and one-step optimize-and-send.
