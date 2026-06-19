<p align="center">
  <img src="docs/banner.png" alt="PDF FAX — maximize document quality over any fax line" width="100%">
</p>

# PDF FAX — an Agent Skill

<p align="center">
  <img alt="PyPI: package not published" src="https://img.shields.io/badge/pypi-package%20not%20published-red.svg">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue.svg">
  <img alt="Claude + Codex" src="https://img.shields.io/badge/Claude%20%2B%20Codex-agent%20ready-555555.svg">
  <img alt="Agent Skill (SKILL.md)" src="https://img.shields.io/badge/Agent%20Skill-SKILL.md-orange.svg">
  <img alt="Formats: PDF, DOCX, PPTX, XLSX, image" src="https://img.shields.io/badge/formats-.pdf%20%7C%20.docx%20%7C%20.pptx%20%7C%20.xlsx%20%7C%20image-777777.svg">
  <img alt="Output: fax PDF, TIFF, JSON, PNG preview" src="https://img.shields.io/badge/output-.fax.pdf%20%7C%20.tiff%20%7C%20.json%20%7C%20.png-8A2BE2.svg">
</p>

A portable [Agent Skill](https://www.anthropic.com/news/skills) that teaches an
AI coding agent to **maximize document quality and readability when sending a
PDF over a fax network.** It converts a PDF into a fax-native **1-bit bilevel
CCITT-G4** PDF (or Class-F multipage TIFF) that survives the lossy Group-3
transmission and **arrives legible on the receiving machine.**

> **A fax's whole job is to be READ.** That is the single most important thing
> about this skill. Fax transmission is low-resolution, 1-bit, and lossy by
> design over a noisy phone line, so this skill optimizes for **legibility on
> the other end first** — crisp text, intact small fonts and signatures,
> recognizable photos. Smaller files and faster transmission are welcome side
> effects, never the goal: a tiny fax that arrives unreadable is a failure.

> **Just need to shrink a PDF for email or the web?** That's a different job with
> the opposite trade-offs — use the companion skill:
> **[pdf-email-optimizer](https://github.com/petehottelet/pdf-email-optimizer)**.

To make a fax legible, the skill models the Group-3 constraint (1-bit,
run-length compression along each scanline) and runs an OCR-driven, layered
pipeline: it renders at the **source's native resolution with square pixels**
(no aspect distortion, no down-scaling), segments image areas, runs OCR over
the page, and recolors **every recognised word's glyphs to SOLID BLACK or SOLID
WHITE by a single bright-line rule — median field luminance vs `#808080`**.
The recoloured glyphs ride a **text layer composited above** the halftoned
image layer, so the screen can never disturb them. It defends fine detail
(background flatten, despeckle, deskew, optional stroke thickening), warns
about content that won't survive bilevel, and lets you **preview exactly what
will be transmitted** with a 4-panel sample sheet so you can confirm it's
readable before sending.

The `SKILL.md` format is an open standard. This skill is built and tested for
**Claude** (Claude Code / claude.ai) and **OpenAI Codex**.

## What it does

- Accepts **PDF, Word, PowerPoint, Excel, OpenDocument, text, and image** input,
  normalizing non-PDF formats to PDF first (see *Input formats* below).
- Rasterizes each page at the **source's native resolution with square pixels**
  — raster sources (PNG, JPG, scans) come through pixel-for-pixel with their
  original aspect ratio; vector sources rasterize square at the
  `--fax-resolution` preset (default **`superfine`**).
- MRC-lite segmentation using the PDF's embedded-image rectangles: photos go to
  halftone, document text goes to an adaptive binarizer, with a guard for
  full-page rasters so the document's own text never gets dithered.
- **OCR-driven text recolor** (default on): `rapidocr-onnxruntime` locates
  every word — outside images (`--ocr-text`) and, with `--recover-text`, inside
  images — and the pipeline marks each word BLACK or WHITE by the **#808080
  rule** (median field luma < 128 → WHITE; ≥ 128 → BLACK). The recoloured
  glyphs ride a layer composited **above** the halftone, so the screen can
  never disturb them.
- Pre-cleans: background flatten, despeckle, deskew; optional stroke thickening
  to save hairlines and small fonts.
- Emits lossless CCITT-G4 (no re-encode) via img2pdf — a `CCITTFaxDecode` PDF or
  a Class-F multipage TIFF, with the (square) effective DPI embedded so the
  output PDF is correctly proportioned.
- `--transmission-safe` clamps the final scanline to 1728 px when you need
  strict Group-3 transmissibility (default keeps native resolution for
  legibility).
- Produces a JSON report with **estimated transmission time per page**, every
  recoloured word and its polarity, and legibility/inversion warnings — plus
  `--sample N` for a 4-panel preview sheet (original / grayscale /
  halftone-only / halftone+recover-text).

## Optimizing for the channel, not "fax-ifying" the document

The goal is to **optimize the document for transmission**, not to make it look
like a generic fax. The skill treats a page as Mixed Raster Content and applies a
*different, selectable schema* to each kind of content:

- **Text / line art** → a contrast-maximizing binarizer (`--text-binarize`,
  default `contrast`; also `sauvola`, `niblack`, `wolf`, `bradley`, `otsu`).
  Text is **never halftoned** — it is thresholded for legibility, pulling gray /
  light-gray text on white to **solid black**, and holding glyphs crisp over dark
  header bars, reverse type, and uneven illumination where a single global cut
  drops light text or fills shadows.
- **Photos / continuous tone** → a **halftone schema** (`--dither`), with
  **dot-gain pre-correction** (`--tone-curve auto`, so midtones don't plug to a
  black silhouette) and optional **edge sharpening** (`--sharpen`).
- **Text baked into an image** (captions, signs, screenshots, or a whole page
  scanned as a single image) → detected *inside* the photo region and routed back
  to the legibility path, so it stays readable instead of dissolving into a
  halftone screen (`--no-text-in-image` to disable). See the next section.

### Text inside images — found, recolored by the #808080 rule, kept legible

Plenty of real fax jobs are full of text that *isn't* live text: a whole page
exported or scanned as a single image, a screenshot, a caption burned into a
photo, a sign in a snapshot. If that page were treated as one big picture and
halftoned, the words would dissolve into dot-screen mud. The pipeline therefore
runs **OCR over every page** and recolors text by a single bright-line rule —
the **#808080 rule** — and never lets the halftone touch a glyph:

1. **Locate every word.** OCR (`rapidocr-onnxruntime`) finds words in two
   scopes: OUTSIDE the image regions (`--ocr-text` — the page's
   header/footer/form text) and, with `--recover-text`, INSIDE the image
   regions (signage, captions). OCR is the locator only; if the engine isn't
   installed the skill falls back to the binarizer's default black-on-white.
2. **Segment the original glyph pixels.** Each word's OCR box is used to crop;
   glyphs are split from their field using the box's border ring (definitely
   field) — robust where a blind 2-means split would invert. The real
   letterforms are preserved; no synthetic font is ever pasted.
3. **Pick polarity per the #808080 rule.** Median field luminance < 128
   ⇒ glyphs become **WHITE**; ≥ 128 ⇒ glyphs become **BLACK**. Polarity is
   decided **per sign** (words grouped by proximity, one field tone for the
   whole group) so co-located words on the same plate get one consistent
   treatment.
4. **Composite text ABOVE the halftone.** The recoloured glyphs ride a text
   layer painted on top of the halftoned image layer, so the halftone screen
   can never disturb them. No field-lift, no field-darken, no stroke backing —
   the layered composite makes those obsolete.

The payoff: **even when an image is aggressively halftoned for transmission,
every word inside it stays legible** — and the report lists each recoloured
word with its OCR confidence, polarity, and field luminance so you can verify.
A `--no-text-in-image` fallback is also available for the rare case where you
want pure halftone everywhere.

### Halftone schemas + the "eye tokens" comparison preview

A continuous-tone photo can't exist in 1-bit fax — it has to be simulated with
dot patterns, and that choice is the biggest lever on how a photo reads after a
lossy transmission. The skill ships these schemas, spanning the design space:

| `--dither` | Family | Detail | G4 size | Noise robustness |
|---|---|---|---|---|
| `clustered` | AM screening — round clustered-dot | low–med | **best** | **best** |
| `screen --dot-shape square` | AM screening — square dot | low–med | **best** | **best** |
| `screen --dot-shape diamond` | AM screening — diamond dot (newspaper-photo classic) | low–med | **best** | **best** |
| `screen --dot-shape ellipse` | AM screening — ellipse dot (smooth midtone joins) | low–med | **best** | **best** |
| `mezzotint` | AM grain — random stippling (expressive; not eligible for `auto`) | med | **worst** | low |
| `ordered` | Bayer ordered (matrix dither) | med | med | med |
| `blue-noise` | FM screening (void-and-cluster) | **high** | medium | medium |
| `green-noise` | hybrid AM–FM (clustered FM) | med–high | good | good |
| `floyd` | error diffusion (4-tap Floyd-Steinberg) | **highest** | **worst** | **worst** |
| `atkinson` | error diffusion (6/8 Atkinson, clean whites) | high | med | low–med |
| `jarvis` | error diffusion (12-tap Jarvis-Judice-Ninke, very smooth) | high | poor | low–med |
| `stucki` | error diffusion (12-tap Stucki, sharp + smooth) | high | poor | low–med |
| `sierra` | error diffusion (12-tap Sierra, lighter Jarvis) | high | poor | low–med |
| `edd` | edge-enhancing error diffusion (high-pass + diffusion) | high | med | low–med |
| `line` (`woodcut`) | horizontal line screen (engraving) | med | **best** | **best** |
| `crosshatch` | layered angled line screens (pen-and-ink etching) | med | good | good |
| `none` | hard threshold (no halftone) | — | **best** | **best** |

`green-noise` is the standout addition for a real fax line — blue-noise detail
with clustered-dot run-length/robustness, tunable via `--green-noise-coarseness`
(~2 detail … 8 robust). `line`/`woodcut` renders tone as horizontal stripes that
thicken with darkness — because the strokes run *along the scanline* it is the
most G4-friendly way to carry a photo and reads as a clean engraving, never mud.
Because the pipeline runs at square pixels, the screens are isotropic by
construction — dots stay round on paper without any anisotropic correction.
`mezzotint` is an expressive screen — it gives velvety midtones but has no
spatial coherence, so it compresses poorly and isn't eligible for the auto-picker;
it has to be requested explicitly.

Every screen in the registry, applied to the same letter cover sheet —
**`floyd`, `jarvis`, and `edd` are highlighted as the OPTIMAL picks** for a
forms-and-photo page like this one (preserve fine document text, hold
photographic detail in the masthead, keep edge sharpness on the billboard):

<p align="center">
  <img src="docs/readme/halftone_grid.png" alt="Halftone style contact sheet — every screen in the registry applied to the same Prestige Estates cover sheet, with floyd / jarvis / edd marked OPTIMAL for a forms-and-photo page" width="100%">
</p>

That grid above is the whole catalogue. For picking by eye on your own document
you don't need all 17 — compression can be ranked by a machine, but **readability
can't**, and you can't read 17 thumbnails in parallel. So `--compare-page N`
renders one page of your file through a **curated 6-up subset** of those screens
(`clustered`, `green-noise`, `blue-noise`, `atkinson`, `floyd`, `line` — one
representative per family) into a single labeled **contact sheet**, each panel
annotated with its real G4 size and transmission estimate and the recommended
pick highlighted. The skill **suggests the optimal** method from the page's
content, and you **choose the optimal** by spending your *eye tokens* on the
contact sheet — then re-run with the chosen `--dither` for the final file.

```bash
python pdf-fax-optimizer/scripts/optimize_pdf.py input.pdf -o output.fax.pdf \
    --compare-page 1
# -> writes output.fax.compare_p1.png (a 6-up of clustered/green-noise/
#    blue-noise/atkinson/floyd/line so you can pick the panel that reads best)

# Add --compare-original to lead with two reference panels — the original in
# color (#1) and a true grayscale of it (#2) — followed by four halftones:
python pdf-fax-optimizer/scripts/optimize_pdf.py input.pdf -o output.fax.pdf \
    --compare-page 1 --compare-original
```

### Text That Survives the Fax

Two layered passes protect text against the 1-bit channel. Both are on by
default and the report lists everything they touched so you can verify.

**`preserve_text` — dark text on any colored fill (no OCR needed).** Slide
labels on highlight chips, dashboard status badges, colored table cells,
tinted callout boxes, color-filled banners — anywhere dark text sits on a
saturated-colour background. In grayscale those fills collapse to mid-tone
luma and the contrast binarizer flips polarity, painting the fill solid
black and knocking the label out as a mangled crosshatch. The `preserve_text`
pass runs ahead of the binarizer: small high-chroma regions that carry dark
text strokes get **lifted to white in the gray image** so the dark text reads
as crisp black-on-white. Disable with `--no-preserve-text`.

**`recover_text` — OCR-driven #808080 polarity for text inside photos.** OCR
locates every word inside the photo region; each word's ORIGINAL glyph pixels
are recoloured BLACK on a light/mid field or WHITE on a dark field by the
#808080 rule, then composited **above** the halftone so the screen never
disturbs them. Opt in with `--recover-text on`.

<p align="center">
  <img src="docs/readme/text_rescue.png" alt="Text rescue features — top row: preserve_text whitens colored highlight chips so dark labels read cleanly through the 1-bit threshold; bottom row: recover_text OCRs text baked into the halftoned billboard and recolors it BLACK or WHITE by the #808080 rule" width="100%">
</p>

A deeper recover_text demo — the same **VILLA DEL SOL** billboard rendered
through six halftone choices, with the OCR'd gold sign text **knocked out to
white** above each halftone screen. The polarity is content-driven: the
#808080 luma rule paints recovered glyphs **black** when they sit on light
backgrounds and **white** when they sit on dark ones, so the sign text reads
crisply against either side of the halftoned billboard:

<p align="center">
  <img src="docs/compare_example.png" alt="Six-panel fax comparison showing original color, true grayscale washing out the gold sign text into the dark billboard background, and four fax halftone treatments with the OCR-recovered sign text knocked out to white above each halftone screen so it reads crisply on the dark billboard" width="100%">
</p>

## Input formats — fax a PDF, or a Word, PowerPoint, Excel, or image file

You don't have to start from a PDF. Point the optimizer at common office and
image formats and it normalizes them to PDF first, then runs the exact same
fax pipeline:

- **Word / OpenDocument / text** — `.doc`, `.docx`, `.rtf`, `.odt`, `.txt`
- **PowerPoint** — `.ppt`, `.pptx`, `.odp`
- **Excel / CSV** — `.xls`, `.xlsx`, `.ods`, `.csv`
- **Images** — `.png`, `.jpg`, `.tif`, `.bmp`, `.gif`, `.webp`
- **PDF** — used as-is

```bash
# Fax a Word doc directly (defaults: superfine + OCR-driven #808080 polarity)
python pdf-fax-optimizer/scripts/optimize_pdf.py proposal.docx -o proposal.fax.pdf

# Fax a scanned image with a 4-panel sample sheet
python pdf-fax-optimizer/scripts/optimize_pdf.py scan.jpg -o scan.fax.pdf --sample 1
```

Images are wrapped to PDF with `img2pdf` (no extra tools). Office/OpenDocument
files are rendered by **LibreOffice headless** (`soffice`), which reproduces the
layout faithfully — install [LibreOffice](https://www.libreoffice.org/download/)
once (it needs no GUI) or export to PDF yourself. Add `--keep-converted-pdf` to
retain the intermediate PDF next to the output.

## Repository layout

```
.
├── README.md              # this file (for humans)
├── LICENSE                # MIT
├── requirements.txt       # Python deps
└── pdf-fax-optimizer/         # the skill (this folder IS the skill)
    ├── SKILL.md           # entry point: metadata + instructions
    ├── agents/
    │   └── openai.yaml     # optional Codex UI sidecar
    ├── assets/
    │   ├── bluenoise_64.npy # cached void-and-cluster blue-noise matrix
    │   └── Oswald.ttf       # bundled display font for the comparison title
    ├── scripts/
    │   ├── check_deps.py   # verify/install dependencies
    │   ├── optimize_pdf.py # CLI entry point (optimize, and optionally --send)
    │   ├── fax_pipeline.py # the fax conversion pipeline
    │   ├── to_pdf.py       # normalize Office/image input to PDF
    │   └── send_fax.py     # transmit via a cloud fax API (mFax/Phaxio/generic)
    └── references/
        ├── fax-optimization.md  # the Group-3 model + why each knob exists
        ├── config-schema.md     # JSON config schema + examples
        └── sending.md           # send via a cloud fax API
```

## Requirements

- **Python 3.9+** with: PyMuPDF, Pillow, numpy, opencv-python-headless, img2pdf
  (`pip install -r requirements.txt`). `requests` is also installed, needed only
  to **send** faxes.
- **`rapidocr-onnxruntime`** (optional but recommended) — drives the OCR-based
  #808080 polarity passes (`--ocr-text` and `--recover-text`). Self-contained
  (bundled ONNX models, no system OCR binary). Without it the skill still
  works: document text falls back to the binarizer's default black-on-white
  and the within-image recover pass is silently skipped.
- **No CLI tools required** for PDF/image input. (qpdf / Ghostscript are optional
  and only useful for unrelated PDF work.)
- **LibreOffice** (optional) — only needed to fax **Office/OpenDocument** input
  (Word/PowerPoint/Excel); it runs headless, no GUI.

Let the skill bootstrap the Python side:

```bash
python pdf-fax-optimizer/scripts/check_deps.py   # installs missing pip deps
```

## Installing the skill

`SKILL.md` is the open standard; the only difference between agents is **where**
the skill folder lives. Copy the `pdf-fax-optimizer/` folder into the appropriate
location:

| Agent | Location (user-level) | Location (project-level) |
|---|---|---|
| **Claude Code** | `~/.claude/skills/pdf-fax-optimizer/` | `.claude/skills/pdf-fax-optimizer/` |
| **OpenAI Codex** | `~/.codex/skills/pdf-fax-optimizer/` | `.agents/skills/pdf-fax-optimizer/` |

```bash
git clone https://github.com/petehottelet/pdf-fax-optimizer.git
# Claude Code
cp -r pdf-fax-optimizer/pdf-fax-optimizer ~/.claude/skills/pdf-fax-optimizer
# OpenAI Codex
cp -r pdf-fax-optimizer/pdf-fax-optimizer ~/.codex/skills/pdf-fax-optimizer
```

**Claude Code** discovers skills automatically (no restart) and you can invoke
with `/pdf-fax-optimizer`. For **claude.ai** (web/desktop), zip the `pdf-fax-optimizer/`
folder so the folder is the archive root, then upload it under
Settings → Capabilities → Skills:

```bash
cd pdf-fax-optimizer && zip -r pdf-fax-optimizer.zip pdf-fax-optimizer
```

**OpenAI Codex** keeps skills behind an experimental flag — enable it once, then
restart Codex:

```toml
# ~/.codex/config.toml
skills = true
```

Codex activates the skill implicitly when your request matches the description,
or explicitly via `$pdf-fax-optimizer`. (Codex caps the frontmatter `description` at
500 characters — this skill's description is within that limit.)

## Using it directly (without an agent)

The scripts are a normal CLI:

```bash
# Make a PDF faxable (default: superfine, native res, OCR + #808080 on)
python pdf-fax-optimizer/scripts/optimize_pdf.py input.pdf -o output.fax.pdf \
    --report output.report.json --sample 1

# Compare the 6-up of halftone methods on page 1 and pick by eye
python pdf-fax-optimizer/scripts/optimize_pdf.py input.pdf -o output.fax.pdf \
    --compare-page 1

# Strict Group-3 transmissibility (1728-px scanline) for a real fax machine
python pdf-fax-optimizer/scripts/optimize_pdf.py input.pdf -o output.fax.pdf \
    --transmission-safe

# Multipage Class-F G4 TIFF instead of a PDF
python pdf-fax-optimizer/scripts/optimize_pdf.py input.pdf -o output.tiff \
    --format tiff
```

See `pdf-fax-optimizer/references/config-schema.md` for the full flag/config
reference, and `pdf-fax-optimizer/references/fax-optimization.md` for the reasoning
behind the fax defaults.

## Sending the fax via a cloud API

The skill can also **transmit** the optimized file — no machine, modem, or phone
line, just an API key and the recipient number in **E.164**. Built-in providers:
`mfax` (mFax/Documo), `phaxio` (Phaxio/Sinch), and `generic` (any upload API such
as Telnyx or SRFax). Always pass keys via environment variables, and use
`--dry-run` to preview the exact request first.

```bash
export MFAX_API_KEY=sk_live_xxx

# optimize and send in one step (transmission-safe for real fax lines)
python pdf-fax-optimizer/scripts/optimize_pdf.py input.pdf -o output.fax.pdf \
    --transmission-safe \
    --send mfax --to +14155551234 --dry-run     # drop --dry-run to transmit

# or send an already-optimized file
python pdf-fax-optimizer/scripts/send_fax.py output.fax.pdf \
    --provider phaxio --to +14155551234
```

See `pdf-fax-optimizer/references/sending.md` for per-provider endpoints, auth, env
vars, and configuring `generic` for other APIs.

## License

MIT — see [LICENSE](LICENSE).
