# PDF Optimizer — an Agent Skill for email & fax

A portable [Agent Skill](https://www.anthropic.com/news/skills) that teaches an
AI coding agent to optimize PDFs for a **specific delivery channel**:

- **`size` mode** — shrink a PDF for email/web (downsample over-resolution
  images, re-encode, qpdf structural cleanup, linearize for fast web view).
- **`fax` mode** — convert a PDF into a fax-native **1-bit bilevel CCITT-G4**
  PDF (or Class-F multipage TIFF) that survives Group-3 transmission *and stays
  legible*.

The skill is built around one idea: the right trade-offs for "email it" are the
opposite of the right trade-offs for "fax it." Fax mode in particular models the
Group-3 constraint (1-bit, anisotropic resolution, run-length compression along
each scanline) and does MRC-lite segmentation — crisp hard-thresholded text,
halftoned photos — instead of dithering the whole page into mud.

The `SKILL.md` format is an open standard, so the same skill works in **Claude
Code, Claude.ai, OpenAI Codex, Gemini CLI, GitHub Copilot**, and other
compatible agents.

## What it does (fax mode highlights)

- Rasterizes each page at a fax-native resolution (`standard` 204×98, `fine`
  204×196, `superfine` 204×391), resampling axes independently and clamping the
  scanline to 1728 px.
- MRC-lite segmentation using the PDF's embedded-image rectangles: text/line-art
  → hard threshold; photos → halftone (Floyd, Atkinson, ordered, or clustered).
- Pre-cleans: background flatten, despeckle, deskew; optional stroke thickening
  to save hairlines and small fonts.
- Emits lossless CCITT-G4 (no re-encode) via img2pdf; qpdf linearizes the result.
- Produces a JSON report with **estimated transmission time per page** and
  legibility/inversion warnings, plus a `--preview-page` PNG of exactly what
  will be transmitted.

## Repository layout

```
.
├── README.md              # this file (for humans)
├── LICENSE                # MIT
├── requirements.txt       # Python deps
└── pdf-optimizer/         # the skill (this folder IS the skill)
    ├── SKILL.md           # entry point: metadata + instructions
    ├── agents/
    │   └── openai.yaml     # optional Codex UI sidecar
    ├── scripts/
    │   ├── check_deps.py   # verify/install dependencies
    │   ├── optimize_pdf.py # CLI entry point (size / size-lossless / fax)
    │   └── fax_pipeline.py # the fax conversion pipeline
    └── references/
        ├── fax-optimization.md      # the Group-3 model + why each knob exists
        ├── general-optimization.md  # size-reduction procedure
        └── config-schema.md         # JSON config schema + examples
```

## Requirements

- **Python 3.9+** with: PyMuPDF, Pillow, numpy, opencv-python-headless, img2pdf
  (`pip install -r requirements.txt`).
- **qpdf** (CLI) for lossless structural optimization and linearization.
- **Ghostscript** (optional) as an alternative rasterizer.

Install the system tools:

```bash
# macOS
brew install qpdf ghostscript

# Debian/Ubuntu
sudo apt-get install qpdf ghostscript

# Windows (winget)
winget install qpdf.qpdf
```

Or just let the skill bootstrap the Python side:

```bash
python pdf-optimizer/scripts/check_deps.py   # installs missing pip deps, reports qpdf/gs
```

## Installing the skill

`SKILL.md` is the open standard; the only difference between agents is **where**
the skill folder lives. Copy the `pdf-optimizer/` folder into the appropriate
location:

| Agent | Location (user-level) | Location (project-level) |
|---|---|---|
| **Claude Code** | `~/.claude/skills/pdf-optimizer/` | `.claude/skills/pdf-optimizer/` |
| **OpenAI Codex** | `~/.codex/skills/pdf-optimizer/` | `.agents/skills/pdf-optimizer/` |
| **Gemini CLI** | `~/.gemini/skills/pdf-optimizer/` | `.gemini/skills/pdf-optimizer/` |

```bash
git clone https://github.com/petehottelet/PDF-fax-optimizer.git
# Claude Code
cp -r PDF-fax-optimizer/pdf-optimizer ~/.claude/skills/pdf-optimizer
# OpenAI Codex
cp -r PDF-fax-optimizer/pdf-optimizer ~/.codex/skills/pdf-optimizer
```

**Claude Code** discovers skills automatically (no restart) and you can invoke
with `/pdf-optimizer`.

**OpenAI Codex** keeps skills behind an experimental flag — enable it once, then
restart Codex:

```toml
# ~/.codex/config.toml
skills = true
```

Codex activates the skill implicitly when your request matches the description,
or explicitly via `$pdf-optimizer`. (Codex caps the frontmatter `description` at
500 characters — this skill's description is within that limit.)

**Claude.ai (web/desktop):** zip the `pdf-optimizer/` folder so the folder is the
root of the archive, then upload it under Settings → Capabilities → Skills.

```bash
cd PDF-fax-optimizer && zip -r pdf-optimizer.zip pdf-optimizer
```

## Using it directly (without an agent)

The scripts are a normal CLI:

```bash
# Make a PDF faxable (1-bit CCITT-G4, fine resolution) + report + preview
python pdf-optimizer/scripts/optimize_pdf.py input.pdf -o output.fax.pdf \
    --mode fax --fax-resolution fine --dither auto \
    --report output.report.json --preview-page 1

# Shrink a PDF for email
python pdf-optimizer/scripts/optimize_pdf.py input.pdf -o output.pdf \
    --mode size --target-dpi 150 --jpeg-quality 75 --linearize

# Lossless structural shrink only (vector/text-heavy PDFs)
python pdf-optimizer/scripts/optimize_pdf.py input.pdf -o output.pdf \
    --mode size-lossless
```

See `pdf-optimizer/references/config-schema.md` for the full flag/config
reference, and `pdf-optimizer/references/fax-optimization.md` for the reasoning
behind the fax defaults.

## License

MIT — see [LICENSE](LICENSE).
