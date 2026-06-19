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
  Office/OpenDocument files (Word/PowerPoint/Excel) needs LibreOffice (headless);
  the OCR-driven text-polarity passes (--ocr-text and --recover-text) need
  rapidocr-onnxruntime (self-contained, no system binary). Without OCR the
  skill still works — it falls back to the binarizer's default black-on-white
  for document text and skips the within-image recover-text recolor. No CLI
  tools are required for PDF/image input. Run scripts/check_deps.py first; it
  installs the pip packages if missing.
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
gray), compressed with run-length codes (CCITT G3/G4) *along each scanline*.
That last fact is the hinge for every halftone decision — anything that creates
many black↔white transitions per line compresses badly, transmits slowly, and is
more vulnerable to line noise. Fax mode is fundamentally a balance between
**visual fidelity** and **transition density**.

The pipeline runs at the **source's NATIVE resolution with SQUARE pixels,
hard-capped at 300 PPI** — raster input (a PNG/JPG, a scanned page) comes
through pixel-for-pixel below the ceiling and bicubic-downsamples to 300 PPI
above it, **never upscaled, never anisotropically stretched.** 300 PPI is the
legibility plateau of the 1-bit fax channel: a fine fax preset is 196 lpi
vertical and super-fine is 391 lpi vertical anisotropic, and a square 300-PPI
grid exceeds both effective resolutions while keeping page buffers at ~8 MP
for letter-size. Upscaling a wrapped raster bicubic-interpolates its baked-in
text and soft letterforms break the binarizer ("ABC" reads as "A8C"); a raster
source's text quality is *fundamentally* bounded by its own pixel grid, so the
pipeline honours that exactly. The `--fax-resolution` preset (`fine` /
`superfine`, **default `superfine`**) sets the halftone screen detail and the
rasterization DPI for **vector PDFs only** (whose glyphs are real vectors the
renderer can rasterise crisp at any DPI), again clipped to the 300-PPI
ceiling. At low native DPI the recommender auto-picks an FM screen
(`blue-noise` / `atkinson`) — its dot pitch is the pixel itself, with no
multi-pixel cell, so the screen looks fine and organic where a clustered cell
would collapse to a chunky 24-lpi magazine pitch. Pass `--transmission-safe`
to clamp the final output back to a 1728-px-wide Group-3 scanline if you need
to send through a real fax machine.

Run it:

```bash
python3 scripts/optimize_pdf.py INPUT.pdf -o OUTPUT.pdf \
    --sample 1 --report OUTPUT.report.json     # superfine + OCR by default
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
python3 scripts/optimize_pdf.py proposal.docx -o proposal.fax.pdf  # Word → fax PDF
```

What the pipeline does, per page (details in the reference):

1. **Rasterize at NATIVE resolution, SQUARE pixels, capped at 300 PPI.** Raster
   input (a PNG/JPG, a scanned page) renders at its native pixels-per-inch
   exactly when below the ceiling — pixel-for-pixel with the source, no
   bicubic upsample to "lift" it — and downsamples cleanly to 300 PPI above
   it. Vector pages rasterise square at the preset DPI (`fine` 196 /
   `superfine` 391, default `superfine`), again clipped to 300 PPI. The
   (square) effective DPI is embedded in the output TIFF/PDF so the page is
   correctly proportioned, and the 300-PPI ceiling keeps per-page buffers at
   ~8 MP for letter-size regardless of source resolution.
2. **Segment content (MRC-lite).** Photo/continuous-tone regions are isolated
   (PDF embedded-image rectangles + a variance heuristic for full-page wrapped
   rasters). For colour sources a **chroma gate** also fires: a candidate
   block has to carry real colour to be classified as photo, which keeps
   grayscale form-table cells, message bodies, and printed text rows out of
   the halftone path (their variance alone is indistinguishable from a real
   photo). The chroma gate self-disables on grayscale sources. Document text
   and line art outside the photo region go to the adaptive binarizer; the
   photo region goes to a halftone schema. *Never dither text.*
3. **OCR (off by default).** The OCR engine is the slow step (~20 min on a
   6-page 391-DPI deck) and is **only run when `--recover-text` is on**. When
   it runs it provides the #808080 polarity recolour in two scopes:
   - **Document text OUTSIDE images** (header/footer/form text). Every
     recognised word is bucketed by the **#808080 rule**: median field
     luminance below 128 → glyphs marked WHITE; at or above 128 → glyphs
     marked BLACK. (`--ocr-text off` keeps the OCR pass but skips this
     scope.)
   - **Text INSIDE images** (`--recover-text on`). Same #808080 rule, scoped
     to the photo region. Each word's ORIGINAL glyph pixels are segmented
     from the colour image and marked BLACK or WHITE per its sign's median
     field — preserving real letterforms, never retypesetting.

   Without OCR (the default) the chroma-aware photo segmenter keeps form/
   body/footer text out of the halftone path and the adaptive binarizer's
   contrast threshold pulls dark text to solid black against any near-white
   field — which is correct for the overwhelmingly common case. OCR is the
   escape hatch for white-on-coloured headers, signage inside photos, and
   captions baked into screenshots.
4. **Preserve-text rescue (default on, no OCR needed).** When dark text
   sits on *any saturated-colour fill* — a highlight chip behind a slide
   label, a tinted callout box, a colored table cell, a status badge in a
   dashboard, a filled banner, a colored form field — the bright fill
   collapses to a mid-tone in grayscale and the contrast binarizer flips
   polarity, painting the field solid black and knocking the text out as
   a mangled crosshatch. The preserve-text pass runs ahead of the
   binarizer: small (`< 4%` of page), high-chroma, low-variance regions
   that carry dark text strokes get **lifted to white in the gray image**
   so the dark text reads as crisp black-on-white through the standard
   binarize path. The "this is a highlight" colour cue is sacrificed —
   on 1-bit fax it was going to die anyway — and the text survives.
   Large colour panels and photo content are protected by the area cap
   and the `~photo` mask; the rescue never touches the photo region.
   Disable with `--no-preserve-text` if you'd rather keep the fill as a
   halftoned tone band.
5. **Pre-clean:** flatten near-white backgrounds to pure white, despeckle, and
   deskew (the photo mask and text masks ride the deskew rotation).
6. **Build the bilevel page in three layers, composited in this order:**
   - **Image layer** — halftone the photo region (with dot-gain pre-
     correction; `--dither auto` picks the schema).
   - **Document layer** — adaptive binarization for everything else, so vector/
     document text stays crisp solid black.
   - **Text layer (ABOVE the others)** — the OCR-driven recolor: BLACK pixels
     forced to 0, WHITE pixels forced to 255. Because text rides this layer,
     the halftone screen can never disturb glyphs.
7. **Defend legibility:** optionally thicken hairlines, warn on inverted /
   heavy-black pages.
8. **Emit CCITT G4** inside the PDF (default) or a Class-F multipage TIFF
   (`--format tiff`) — losslessly, via img2pdf, never re-encoded. Pass
   `--transmission-safe` to also clamp the scanline to 1728 px.
9. **Report** per-page transmission estimate, recoloured words (with their
   confidences and polarities), total pages, and any warnings — so the result
   is inspectable *before* someone faxes something unreadable.

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
`none` = hard threshold for pure text / line art / barcodes.) Because the
pipeline runs at square pixels, the screens are isotropic by construction —
dots stay round on paper without anisotropic correction.

**Generalised AM screen — `--dither screen --dot-shape SHAPE`.** `clustered` is
one specific spot function; the `screen` family exposes the same machinery with
selectable dot shape: `round` (default; byte-identical to `clustered`),
`square` (crisp blocky), `diamond` (the classic newspaper-photo look), or
`ellipse` (smoother midtone joins, chained vertically). `--screen-angle θ`
rotates the dot lattice; off-axis angles trade some G4 compression (shorter
horizontal runs) for the angled-screen aesthetic, and the pipeline logs an
`off_axis_screen:...` warning for `|θ| > 15°`.

**Crosshatch — `--dither crosshatch`.** Layered angled-line screens lit by tone
bands, reading as a pen-and-ink etching. Default `--hatch-angles "0,90"` keeps
transition density close to `line` (compression is *better* than `clustered`
on most pages because each scanline still ends up mostly horizontal runs).
Three or four angles read as a denser engraving but multiply the transition
count along every scanline — the pipeline warns with `crosshatch_dense:...`.

**Mezzotint — `--dither mezzotint` (expressive).** Random-threshold grain — a
velvety, unstructured stippling. Unlike blue-noise FM, mezzotint enforces no
inter-dot spacing, so it has maximum 1-bit transition density per row: **poor
G4 compression and high line-noise sensitivity**. It's not eligible for `auto`
and trips an `expressive_screen:mezzotint` warning if you opt in. Use it when
the artistic surface matters more than channel cost.

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

### Text recolor by the #808080 rule (OCR-driven)

A fax is 1-bit and the receiver re-thresholds on **luminance**. The pipeline
therefore enforces a single, brutally simple legibility contract for every word
on the page: pick BLACK or WHITE by **median field luminance vs `#808080`**
(luma 128) — darker than 128 → text becomes WHITE; at or above 128 → text
becomes BLACK. The recoloured glyphs ride a **text layer composited ABOVE** the
halftoned image layer, so the halftone screen can never disturb them.

OCR (rapidocr-onnxruntime) is the **word locator**, not a typesetter. For every
recognised word the pipeline segments its ORIGINAL glyph pixels from the colour
image (using the OCR box's border ring as the field reference, which is robust
where a blind 2-means split would invert) and stamps *those exact pixels* into
the BLACK or WHITE text mask — preserving the real letterforms, spacing, and
tilt. It never pastes a synthetic font.

The same function runs in two scopes — but the **OCR engine itself only
runs when `--recover-text` is on** (it is the slow step; in default mode the
chroma-aware photo segmenter and the adaptive binarizer handle text routing
without it):

- **`--recover-text`** (default **`off`**) is the master switch — it
  enables the OCR pass. When on it covers text **INSIDE** the image regions
  — signage, captions baked into a photo. Polarity is decided **per sign**
  (words grouped by proximity, one field tone for the whole group), so co-
  located words on the same plate get one consistent treatment. This is
  what brings back the cream "VILLA DEL SOL" lettering on the cyan/gold/
  tan covers even when grayscale flattens it.
- **`--ocr-text`** (default `auto`) is a sub-switch — when recover-text
  is on, this controls whether the same OCR pass *also* recolours text
  **OUTSIDE** the image regions (the page's header/footer/form text on
  coloured bars). Set it `off` to keep the within-image recolour while
  leaving doc text to the binarizer.

```bash
python3 scripts/optimize_pdf.py cover.pdf -o cover.fax.pdf \
    --sample 1                  # 4-panel sheet of the recolor, side-by-side
```

- **Optional OCR.** Both passes need `rapidocr-onnxruntime` (`pip install
  rapidocr-onnxruntime` — self-contained, no system binary). Without it the
  skill still works: document text falls back to the binarizer's default
  black-on-white, and the within-image recover pass becomes a no-op.
- **OCR can misread.** Every recoloured word is listed in the summary and
  report (`recover_text.words` and `ocr_text.words`) with its confidence,
  polarity decision, measured field luminance, and WCAG contrast. **Surface
  these to the user to verify.** Low-confidence words (< `--ocr-conf`, default
  0.5) are dropped.
- `--recover-text off` disables only the within-image pass; document-text
  polarity stays on (it's nearly always desirable).

### The 4-panel sample sheet (`--sample N`)

`--sample N` writes a labeled 4-panel preview so the recolor and halftone
effects can be inspected side by side, without re-running the pipeline four
times. Panels:

1. **ORIGINAL** — the source page in colour, untouched.
2. **GRAYSCALE** — the same page desaturated, no other processing. The
   continuous-tone input the 1-bit channel has to approximate.
3. **HALFTONE on image areas** — bilevel page with halftoning on photo regions
   and document text crisped by the binarizer + OCR-doc-text polarity, but the
   within-image recover pass turned OFF. This shows what signage looks like when
   the channel is left to its own devices.
4. **HALFTONE + RECOVER TEXT** — the full pipeline, with within-image words
   recoloured by the #808080 rule and composited above the halftone.

```bash
python3 scripts/optimize_pdf.py cover.pdf -o cover.fax.pdf --sample 1
# writes cover.fax.sample_p1.png
```

Panels 3 and 4 are rendered with the user's actual options, so the sheet is a
faithful preview of the real fax — not an artificial demo.

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
- For an at-a-glance overview, offer the **4-panel sample sheet** (`--sample N`)
  showing original, grayscale, halftone-only, and full-pipeline side by side.
  This is the fastest way to confirm aspect ratio, that the halftone is on
  image areas only, and that the recoloured text reads.
- When there's real photo content, offer the **comparison contact sheet**
  (`--compare-page N`) so the user can spend their *eye tokens* and pick the
  halftone that reads best — the skill highlights its recommended/optimal pick,
  but the human makes the call.
- When a page has colored signage / brand-colored type, offer the **recover-text
  before/after** (`--recover-text-preview N`) so the user can confirm the
  within-image recolor was helpful.

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
    --transmission-safe \
    --send mfax --to +14155551234 --dry-run     # drop --dry-run to transmit
```

Pass `--transmission-safe` whenever the file will hit a real Group-3 line — it
clamps the rendered scanline to 1728 px so a real fax machine can transmit it.

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
