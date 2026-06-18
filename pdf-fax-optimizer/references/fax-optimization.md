# Fax Optimization Reference

Read this before running the skill. The defaults in the pipeline only make
sense once the Group-3 model below is clear.

## Table of contents

1. The core constraint (why fax is different)
2. Resolution & geometry
3. Tone / image conversion (dithering)
4. Content segmentation (MRC)
5. Legibility preservation
6. Transmission economics
7. Special content & validation
8. Knob → reason map

---

## 1. The core constraint

Group-3 fax (the near-universal standard) is fundamentally:

- **1-bit bilevel** — every pixel is pure black or pure white. There is no gray.
  Any continuous tone must be *simulated* by patterns of black dots (halftoning).
- **Run-length compressed along each scanline** (CCITT/ITU-T: MH/MR/MMR, a.k.a.
  G3 1-D, G3 2-D, and G4). The encoder stores *runs* of same-color pixels per
  line.

That second point drives most non-obvious decisions: **transition density is
cost.** A scanline that alternates black/white frequently has many short runs →
poor compression → longer transmission → more exposure to line errors (and on a
1-D-coded line, an error corrupts the rest of that line). So the whole job is a
balance between *visual fidelity* and *black↔white transitions per line.*

Everything below is a consequence of this.

---

## 2. Resolution & geometry

The pipeline renders at the **source's NATIVE resolution with SQUARE pixels,
hard-capped at 300 PPI**:

- A **raster source** (PNG, JPG, scanned page) renders at its native DPI
  **exactly when below 300 PPI** — pixel-for-pixel with the source, no
  upscale — and bicubic-downsamples to 300 PPI when its native DPI is higher.
  Upscaling a wrapped raster bicubic-interpolates its baked-in text and soft
  letterforms break the binarizer ("ABC" reads as "A8C"); the text quality is
  *fundamentally* bounded by the source's pixel grid, so the pipeline never
  renders above native. Halftone screen fineness at low native DPI is
  recovered by picking an FM screen (`blue-noise` / `atkinson`) — its dot
  pitch is the pixel itself, with no multi-pixel cell, so the screen looks
  fine where a clustered cell would collapse to a chunky 24-lpi magazine
  pitch. The recommender auto-picks the right family by render DPI.
- A **vector source** (a real PDF, an Office document rendered to PDF) has no
  native pixel grid, so it rasterizes square at the chosen preset (its glyphs
  are real vectors the renderer rasterises crisp at any DPI), again clipped
  to the 300-PPI ceiling:

| `--fax-resolution` | Square render DPI (vector pages) | Use |
|---|---|---|
| `fine` | 196 dpi | Good default for mixed documents on a clean line |
| **`superfine`** | **300 dpi** (nominal 391, clipped to ceiling) | **Default — best legibility for small text, signage, photos** |
| `standard` | 98 dpi | Smallest output; text-only memos that don't need detail |

**Why 300 PPI is the cap.** 300 PPI is the legibility plateau of the 1-bit
fax channel: real fax superfine is 196×391 anisotropic, real fine is 196×196
— a square 300-PPI grid exceeds both on the scan-line axis. Rendering higher
just makes the halftone CPU spend longer per pixel for no extra fidelity
that the channel can carry, and a 600-PPI buffer for a 10-inch slide is 20+
megapixels per page, vs. ~5 MP at the ceiling. Per-page buffers stay
predictable: letter-size 8.5×11" → ~8 MP, 10×5.6" slide → ~5 MP, regardless
of source resolution.

The preset only sets:
1. The square DPI **vector pages** rasterize at (clipped to 300 PPI).
2. The halftone screen detail (dot cell size scales with the rendered DPI).

A raster source's pixel grid is the rendered grid, full stop — the preset has
no effect on raster sources.

**Why we no longer use anisotropic fax DPIs.** Real Group-3 lines used
non-square pixels (204×98 / 204×196 / 204×391); rendering anisotropically
matched the wire format but distorted the page when shown on any modern viewer
(square pixels), and forced an aspect-ratio recovery step everywhere. By
rendering square at native resolution we keep the document correctly
proportioned at every stage — TIFF, PDF, preview, sample sheet, OCR — at the
cost that the rendered scanline can exceed the historical 1728-px cap. Pass
**`--transmission-safe`** to clamp the final 1-bit output back to a 1728-px
scanline so a real Group-3 fax machine can transmit it; without that flag the
output is optimized for legibility, not strict G3 transmissibility (which is
the right default in 2026, where most "fax" routes are PDF-over-API anyway).

The (square) effective DPI is embedded in every output TIFF / PDF page, so
img2pdf builds correctly-proportioned PDF pages without anisotropic correction.

---

## 3. Tone / image conversion (dithering)

The algorithm matters more than the decision to "just dither it," because each
choice sits at a different point on the fidelity ↔ transition-density curve.
Transition density is the cost axis: more black↔white transitions per scanline
means worse G4 compression, longer transmission, and more line-noise fragility.

### The top 5 (what the skill ships)

| Schema (`--dither`) | Family | Detail | G4 size | Noise robustness |
|---|---|---|---|---|
| `clustered` | AM screening (clustered-dot) | low–med | **best** | **best** |
| `green-noise` | hybrid AM–FM (clustered FM) | med–high | good | good |
| `blue-noise` | FM screening (void-and-cluster) | **high** | medium | medium |
| `atkinson` | error diffusion (6/8) | high | med | low–med |
| `floyd` | error diffusion | **highest** | **worst** | **worst** |
| `line` (`woodcut`) | horizontal line screen (AM, 1-D) | med | **best** | **best** |

- **Clustered-dot (AM) screening** — like newsprint. Dots grow in clusters, so
  runs are long and compression is far better; it survives transmission and
  re-thresholding best. The cost is lower apparent resolution. `--fax-heavy`
  selects it.
- **Green noise (hybrid AM–FM)** — the best single addition for a marginal
  channel. Built by running void-and-cluster with a *larger* Gaussian sigma so
  the minority pixels coalesce into mid-size clusters: it keeps blue-noise-level
  detail but produces the longer runs and bit-flip tolerance of a clustered
  screen. `--green-noise-coarseness` (~2 → near blue/detail, ~8 → near
  clustered/robust) is the AM↔FM knob. Cached per coarseness in `assets/`.
- **Blue noise (void-and-cluster, FM screening)** — Ulichney's method builds an
  isotropic threshold matrix with energy concentrated at high frequencies. The
  result is an organic stipple with **no directional "worms"** and no clustered
  low-frequency blotches, so it looks clean to the eye and re-thresholds
  gracefully. Compression and robustness land between screening and error
  diffusion.
- **Error diffusion** — Floyd-Steinberg, Atkinson (and the heavier `jarvis`,
  `stucki`, `sierra` kernels, plus `edd` below). Best *perceived* detail because
  quantization error is pushed to neighbors, preserving local average tone. But
  it produces **dispersed, high-frequency speckle** — the worst case for
  run-length compression and the most fragile over a noisy line. Atkinson
  diffuses only 6/8 of the error, giving cleaner whites, better thin-feature
  survival, and slightly better runs than Floyd-Steinberg. The pipeline scans
  serpentine to break up directional worms.
- **`edd` — edge-enhancing error diffusion** (Eschbach): a high-pass term is
  added before diffusion, so glyph/edge structure is sharpened while surrounding
  tone still diffuses. Use it for the gray zone where text sits over a
  photographic background and a hard binarizer would eat either the text or the
  texture.
- **`line` / `woodcut` — horizontal line screen** — a 1-D AM screen: tone is
  rendered as horizontal stripes whose thickness grows with darkness (a
  triangular threshold profile across each vertical period keeps a hairline in
  highlights and fills toward solid in shadows). Because every stroke runs
  *along the scanline*, the output is almost entirely long horizontal runs, so
  G4 size and bit-flip robustness match `clustered` — while the result reads as a
  clean, high-contrast engraving instead of mud. The pitch follows the vertical
  DPI. Ideal when the line is bad and the photo only needs to stay recognizable.
- **Ordered / Bayer (dispersed)** — cheap threshold-map method; between the
  others on both axes. Predictable, fast, no error propagation.

**Square pixels.** Because the pipeline renders at square pixels (§2), screens
are isotropic by construction — dots are round on paper without any anisotropic
threshold-tile correction. The legacy aniso-tile helper is now an identity
passthrough.

**Dot-gain pre-correction (`--tone-curve auto`).** A 1-bit channel has large
effective dot gain; without correction, midtones plug to solid black and a photo
arrives as a silhouette — often a bigger win than the dither choice itself. The
pipeline applies a per-family tone curve to the photo region before halftoning
(screens are corrected more than error diffusion). `--sharpen` additionally
unsharp-masks the photo to counter the channel's MTF rolloff.

Decision guide (`recommend_dither`):

- Photo area < 3% (essentially text/line-art) → `none` (hard threshold): sharpest
  and smallest; halftoning would only add noise.
- Photo area > 45% or `--fax-heavy` → **clustered**: keep runs long so it
  compresses and survives a noisy line.
- Otherwise → **green-noise**: blue-noise-level detail but clustered into longer,
  more bit-flip-tolerant runs; drop to `atkinson` for the crispest whites on a
  clean line, or `blue-noise` for a softer isotropic look.

`--dither auto` applies exactly this logic. But the recommendation is only a
starting point — see *Spend your eye tokens* below.

Critically, **match the screen frequency to the fax resolution.** Too fine a
screen for the dpi collapses to mud after the receiving machine re-thresholds.
The pipeline scales the clustered cell size from the target dpi for this reason.

### Spend your eye tokens — the comparison preview

Compression metrics are objective; *readability* is not. Only a human eye can
decide whether a given halftone "reads" for a given document. So the skill can
render one page through all five methods into a single labeled **contact sheet**
(`--compare-page N` → `OUTPUT.compare_pN.png`), each panel annotated with its
real G4 size and transmission estimate, with the recommended pick highlighted.
The agent suggests the optimal method; the user spends their **eye tokens** to
confirm or override it, then the final file is produced with the chosen
`--dither`. Use `--compare-methods a,b,c` to control which methods appear.

---

## 4. Content segmentation (MRC)

The single biggest quality win is *not treating the whole page the same.* This
is the idea behind Mixed Raster Content (MRC): classify regions, then route each
to the right converter.

- **Text & line art** → contrast-maximizing binarization (`--text-binarize`,
  default `contrast`), kept crisp. Text is **never halftoned** — it is
  thresholded for legibility. The default marks black wherever a pixel is darker
  than its local paper level by a small additive margin (`mean − ~13`), which has
  high recall on *light/gray text on white* (it pulls it to solid black rather
  than dropping it, the failure mode of a conservative multiplicative cut) while
  leaving flat, text-free areas clean. `sauvola`/`niblack`/`wolf`/`bradley`
  compute a *per-pixel* threshold from the local mean and standard deviation (via
  integral images, so O(1)/pixel and no contrib dependency), which holds text
  together over dark header bars, reverse (white-on-black) type, and uneven
  scanner illumination where a single global Otsu cut drops glyphs or fills
  shadows. `otsu` remains available as the global fallback.
  - **Halftoning stays on image sections only.** The variance photo mask opens
    with a small kernel first (erasing thin text strokes that fall in the
    mid-variance band), closes to consolidate the genuine photo interior, then
    keeps only large connected components — so gray text never gets welded into a
    photo blob and screened (a top legibility killer).
  - **Solid fills & reverse type** are carried across as *solid* black. Adaptive
    binarizers otherwise misfire on a large dark fill (e.g. a reverse-type header
    bar): inside the fill the local-contrast term pushes the threshold below the
    fill level, so the fill flips to white and only the glyph edges survive as a
    hollow stroke. Where the local mean is dark (a filled region — the mean rides
    out rasterization noise that a flat-variance test trips on) the area is kept
    solid black, and only genuinely bright pixels (knockout/reverse text) stay
    white. Body text on a white page is untouched: its local mean stays light.
  **Never dither text** — dithering destroys edge sharpness and legibility, and
  explodes transition density on exactly the content people most need to read.
- **Photos / continuous-tone** → halftone per §3. Region detection uses the PDF's
  embedded-image rectangles, but a **single image that covers (nearly) the whole
  page** — a scan or an exported cover sheet — is not a photo: trusting that
  rectangle would route the entire document, text and all, to the halftone and
  dither the words into faint dotted mush. So when the embedded mask spans most of
  the page (>80%) the genuine photo is found with the local-variance heuristic and
  then **consolidated into a solid region** — closing small gaps, filling interior
  holes, keeping only large components. That keeps *flat areas inside the photo*
  (a colored sign that greys to a mid tone, a patch of sky) on the halftone path
  instead of binarizing them to stark white, while the page's own text-on-white
  areas, which lie outside the photo, fall back to crisp binarization. If nothing
  reads as continuous-tone, the mask is empty and the whole page is binarized —
  correct for a text-only scan.
- **Text baked into an image** → rescued back to the legibility path. Captions,
  signs, screenshots and (importantly) a whole page scanned as a single image all
  put text *inside* the photo region, where a naive pipeline would halftone it
  into mush. `text_in_image_mask` runs inside the photo region: a top-hat /
  black-hat response isolates thin high-contrast strokes (suppressing smooth
  photo gradients), groups them into horizontal runs, and accepts only
  text-line-shaped components (wide, short, dense). Those pixels keep the
  binarized value instead of the screen. It is conservative by design — it would
  rather miss faint text than carve harsh blobs out of ordinary photo detail —
  and is on by default (`--no-text-in-image` to disable).

The pipeline detects photo regions from the PDF's **embedded raster image
rectangles** (`page.get_image_rects`) rather than guessing from pixels — robust,
and it uses real document structure. Pixels inside those rects get the halftone;
everything else gets the hard threshold. A pure-text page therefore never gets
dithered at all. (For scanned/flattened pages with no embedded-image structure,
fall back to a smoothness/variance heuristic — see `--segmentation variance`.)

---

## 5. Legibility preservation

Bilevel + low dpi quietly kills fine detail, so the pipeline actively defends it:

- **Minimum stroke / font defense.** Hairlines and sub-minimum glyph strokes can
  vanish entirely after thresholding. `--thicken` applies a light morphological
  dilation to thin black features so they survive. Report sub-minimum text.
- **Background flattening.** Push near-white backgrounds to pure white *before*
  thresholding so faint content survives and doesn't get swallowed; conversely,
  drop shaded form/table cell fills (gray fills become halftone noise — usually
  better as white, or a light line pattern).
- **Luminance-aware contrast.** Convert color with proper luminance weighting
  (Rec. 601/709), not a flat channel average. Watch colors that wash out:
  yellow highlights, light blue, light green, and red-on-dark can disappear or
  invert legibility. Warn when a page is dominated by such colors.
- **OCR-driven text recolor (the #808080 rule).** A fax is luminance-only, and
  text that differs from its background mainly in hue (yellow on cyan), or in
  saturation (cream on gold), is the canonical legibility failure: desaturate
  it and the glyphs merge. The fix happens **in colour, before the bilevel
  flatten**, by recoloring every recognised word's ORIGINAL glyph pixels to
  SOLID BLACK or SOLID WHITE per a single bright-line rule:

  > *Median field luminance < 128 ⇒ text becomes WHITE; ≥ 128 ⇒ text becomes
  > BLACK.*

  That is the **#808080 rule** (luma 128 ↔ neutral mid-gray ↔ `#808080`). It is
  applied uniformly to every word, in two scopes:

  - **Document text (OUTSIDE images, `--ocr-text`)** — the page's
    header/footer/form text. Rescues white-on-coloured headers, body text on
    tinted blocks, and any place where the binarizer's default polarity would
    flip the wrong way.
  - **Image text (INSIDE images, `--robust-text`)** — signage, captions baked
    into a photo. Polarity is decided **per sign** (words grouped by
    proximity), so co-located words on the same plate get one consistent
    treatment.

  OCR (rapidocr-onnxruntime) is the **word locator only**, not a typesetter:
  the recogniser's bounding box is used to crop, then each word's glyph pixels
  are segmented from its OCR-box border ring (definitely field) — robust where
  a blind 2-means split would invert. The original letterforms are preserved;
  no synthetic font is ever pasted.

  The recoloured glyphs ride a **text layer composited ABOVE** the halftoned
  image layer, so the halftone screen can never disturb a glyph. There is no
  field-lift, no field-darken, no stroke backing — the layered composite makes
  those obsolete. Every recoloured word is reported under `robust_text.words` /
  `ocr_text.words` with its confidence, polarity decision, and field
  luminance, so the user can verify (OCR can misread).

  Without OCR (`rapidocr-onnxruntime` not installed) the skill still works —
  document text falls back to the binarizer's default black-on-white, the
  within-image robust pass becomes a no-op, and the rest of the pipeline is
  unchanged.

---

## 6. Transmission economics

The output should be cheap and robust to send, not just small on disk:

- **Emit CCITT G4** (or G3 2-D) inside the PDF, or a Class-F multipage TIFF for
  a fax-ready file — *losslessly*, never a thresholded bitmap stored
  uncompressed. The pipeline embeds the G4 stream via img2pdf with no re-encode.
- **Despeckle and deskew before thresholding.** Isolated black pixels waste
  compression and read as dirt; skew turns straight runs into staircases that
  compress worse. Both are cheap pre-cleans with outsized payoff.
- **Estimate transmission time per page.** From the *actual* G4-encoded byte
  size: `seconds ≈ (encoded_bytes × 8) / line_rate + per_page_overhead`, with a
  typical V.17 line rate of 14,400 bps. The report flags "busy" pages likely to
  be slow or error-prone.
- **Detect inverted (white-on-black) content.** Large black areas balloon
  transmission time and stress the line; warn, and offer to invert or flatten.

---

## 7. Special content & validation

- **Barcodes / QR codes** — verify they remain scannable after bilevel
  conversion. They want a **clean hard threshold, never dither**; halftoning
  breaks the quiet zones and module edges. The pipeline keeps detected code
  regions on the threshold path.
- **Signatures / stamps / handwriting** — thin, low-contrast strokes; candidates
  for the contrast + thickening logic in §5.
- **Already-bilevel input** — detect (image is already 1-bit / two-tone) and skip
  reprocessing so you don't dither a clean bitmap into noise.
- **Preview before send.** `--preview-page N` writes a PNG of exactly the
  bilevel output that will be transmitted. Always offer this for fax jobs; it's
  the cheapest insurance against faxing something unreadable.

---

## 8. Knob → reason map

| Flag | Default | Why it exists |
|---|---|---|
| `--fax-resolution` | **`superfine`** | Square halftone screen DPI; raster pages render at native (§2) |
| `--transmission-safe` | off | Clamp final scanline to 1728 px for real Group-3 transmission (§2) |
| `--dither` | `auto` | Photo halftone schema (§3); clustered, green-noise, blue-noise, atkinson, floyd, line/woodcut, ordered, edd, jarvis, stucki, sierra, none |
| `--green-noise-coarseness` | `4.0` | AM↔FM knob for green-noise (~2 detail … 8 robust) (§3) |
| `--text-binarize` | `contrast` | Binarizer for text/line content; `contrast` forces gray/light text to solid black; also sauvola, niblack, wolf, bradley, otsu (§4) |
| `--tone-curve` | `auto` | Per-family dot-gain pre-correction so photos don't plug to black (§3) |
| `--sharpen` | off | Edge-aware unsharp on photo regions before halftoning (§3) |
| `--no-text-in-image` | (rescue on) | Disable fallback rescue of text baked into photos (§4); the OCR-driven `--robust-text` is the primary path |
| `--ocr-text` | `auto` | OCR-driven #808080 polarity for text OUTSIDE images (page header/footer/form text); `off` falls back to the binarizer (§5) |
| `--robust-text` | `auto` | OCR-driven #808080 polarity for text INSIDE images (signage, captions); `off` disables (§5) |
| `--ocr-conf` | `0.5` | Minimum OCR confidence to recolour a word |
| `--sample` | none | Write a 4-panel preview: original / grayscale / halftone-only / halftone+robust-text |
| `--robust-text-preview` | none | Side-by-side PNG faxed WITHOUT vs WITH the within-image robust recolor (§5) |
| `--compare-page` | none | Render the curated 6-up of schemas to one contact sheet so a human can pick by eye (§3) |
| `--compare-original` | off | Lead the sheet with original-color (#1) + true-grayscale (#2) references, then four halftones |
| `--compare-methods` | curated | Override which schemas appear in the comparison |
| `--fax-heavy` | off | Bias to clustered: compresses + survives noisy lines |
| `--segmentation` | `embedded` | MRC routing; `variance` for flattened scans |
| `--thicken` | off | Save hairlines/small fonts from vanishing (§5) |
| `--flatten-bg` | on | Faint content survives thresholding (§5) |
| `--despeckle` | on | Remove dirt; improve compression (§6) |
| `--deskew` | on | Straighten runs; improve compression + legibility |
| `--format` | `pdf` | `tiff` emits Class-F fax-ready multipage G4 |
| `--line-rate` | `14400` | Transmission-time estimate basis (§6) |
| `--preview-page` | none | Inspect actual bilevel output before sending |

---

## Tooling notes

- **PyMuPDF (fitz)** rasterizes pages at the chosen square DPI, enumerates
  embedded images and their rects (used to compute the source's native DPI and
  the photo region), and detects already-bilevel images.
- **Pillow** does Floyd-Steinberg (`convert('1')`) and writes CCITT **G4** TIFF
  (`compression='group4'`).
- **numpy / OpenCV** handle Atkinson/clustered/ordered dithering, background
  flatten, despeckle (morphology), deskew (min-area-rect / projection), and
  thicken (dilation).
- **img2pdf** embeds the G4 TIFF into a PDF *without re-encoding* (the PDF carries
  a `CCITTFaxDecode` filter). No CLI tools are required. It also wraps loose
  **image** input into a one-page PDF at the front of the pipeline.
- **Input normalization (`scripts/to_pdf.py`)** turns non-PDF input into PDF
  before anything else: images via img2pdf; **Word/PowerPoint/Excel/OpenDocument/
  text** via **LibreOffice headless** (`soffice --headless --convert-to pdf`).
  LibreOffice is optional and only needed for those office formats; everything
  downstream is unchanged because it always sees a PDF.
- **rapidocr-onnxruntime** is the optional OCR backend that drives the
  #808080 polarity passes (`--ocr-text`, `--robust-text`). It bundles its own
  ONNX models, no system OCR binary required. When absent the skill silently
  falls back to the binarizer's default black-on-white.
- **Leptonica** (not installed here) is the production-grade choice for
  bilevel/MRC/despeckle if you move this off the current box.
