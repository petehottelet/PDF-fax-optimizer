# Fax Optimization Reference

Read this before running `--mode fax`. The defaults in the pipeline only make
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
- **Fixed, anisotropic resolution** — horizontal is ~204 dpi; vertical is 98,
  196, or 391 dpi depending on mode. The axes differ, so square resampling
  distorts the page.
- **Run-length compressed along each scanline** (CCITT/ITU-T: MH/MR/MMR, a.k.a.
  G3 1-D, G3 2-D, and G4). The encoder stores *runs* of same-color pixels per
  line.

That last point drives most non-obvious decisions: **transition density is
cost.** A scanline that alternates black/white frequently has many short runs →
poor compression → longer transmission → more exposure to line errors (and on a
1-D-coded line, an error corrupts the rest of that line). So the whole job is a
balance between *visual fidelity* and *black↔white transitions per line.*

Everything below is a consequence of this.

---

## 2. Resolution & geometry

Fax-native resolutions (horizontal × vertical dpi):

| Mode | DPI (h×v) | Use |
|---|---|---|
| `standard` | 204 × 98 | Fastest, lowest quality; text-only memos |
| `fine` | 204 × 196 | Good default for mixed documents |
| `superfine` | 204 × 391 | Best quality; small text, photos, fine detail |

Geometry rules the pipeline enforces:

- **Anisotropic resampling.** Render/resample horizontal and vertical axes
  independently to the target dpi. Don't resample to a square grid and stretch.
- **Max scanline width ≈ 1728 px** for A4/Letter at 204 dpi. Never exceed it;
  clamp and re-fit.
- **Standardize page size + safe margins.** Many machines crop the edges, so
  pull content in from the physical edge.
- **Match native resolution exactly to avoid moiré.** If you halftone at one
  pitch and the receiver resamples at another, the two grids beat against each
  other and produce interference patterns. Rendering straight to the fax grid
  avoids this.

---

## 3. Tone / image conversion (dithering)

The algorithm matters more than the decision to "just dither it," because each
choice sits at a different point on the fidelity ↔ transition-density curve.
Transition density is the cost axis: more black↔white transitions per scanline
means worse G4 compression, longer transmission, and more line-noise fragility.

### The top 5 (what the skill ships)

| Method (`--dither`) | Family | Detail | G4 size | Noise robustness |
|---|---|---|---|---|
| `clustered` | AM screening (clustered-dot) | low–med | **best** | **best** |
| `blue-noise` | FM screening (void-and-cluster) | **high** | medium | medium |
| `atkinson` | error diffusion (6/8) | high | med | low–med |
| `floyd` | error diffusion | **highest** | **worst** | **worst** |
| `ordered` | ordered (Bayer) | medium | medium | medium |

- **Clustered-dot (AM) screening** — like newsprint. Dots grow in clusters, so
  runs are long and compression is far better; it survives transmission and
  re-thresholding best. The cost is lower apparent resolution. `--fax-heavy`
  selects it.
- **Blue noise (void-and-cluster, FM screening)** — Ulichney's method builds an
  isotropic threshold matrix with energy concentrated at high frequencies. The
  result is an organic stipple with **no directional "worms"** and no clustered
  low-frequency blotches, so it looks clean to the eye and re-thresholds
  gracefully. Compression and robustness land between screening and error
  diffusion. (The pipeline caches a 64×64 tile in `assets/bluenoise_64.npy`,
  generated numpy-only — no scipy.)
- **Error diffusion** — Floyd-Steinberg, Atkinson (and the heavier `jarvis`,
  `stucki`, `sierra` kernels, selectable but not in the headline 5). Best
  *perceived* detail because quantization error is pushed to neighbors,
  preserving local average tone. But it produces **dispersed, high-frequency
  speckle** — the worst case for run-length compression and the most fragile
  over a noisy line. Atkinson diffuses only 6/8 of the error, giving cleaner
  whites, better thin-feature survival, and slightly better runs than
  Floyd-Steinberg. The pipeline scans serpentine to break up directional worms.
- **Ordered / Bayer (dispersed)** — cheap threshold-map method; between the
  others on both axes. Predictable, fast, no error propagation.

Decision guide (`recommend_dither`):

- Photo area < 3% (essentially text/line-art) → `none` (hard threshold): sharpest
  and smallest; halftoning would only add noise.
- Photo area > 45% or `--fax-heavy` → **clustered**: keep runs long so it
  compresses and survives a noisy line.
- Otherwise → **atkinson**: detail and clean whites without Floyd/Jarvis bloat,
  with `blue-noise` the close runner-up for a softer, isotropic look.

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

- **Text & line art** → hard threshold (Otsu or adaptive), kept crisp.
  **Never dither text** — dithering destroys edge sharpness and legibility, and
  explodes transition density on exactly the content people most need to read.
- **Photos / continuous-tone** → halftone per §3.

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
| `--fax-resolution` | `fine` | Native dpi; anisotropic, avoids moiré |
| `--dither` | `auto` | Fidelity vs. transition-density trade-off (§3); one of clustered, blue-noise, atkinson, floyd, ordered, jarvis, stucki, sierra, none |
| `--compare-page` | none | Render all top-5 methods to one contact sheet so a human can pick by eye (§3) |
| `--compare-methods` | top 5 | Override which halftones appear in the comparison |
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

- **PyMuPDF (fitz)** rasterizes pages (supports anisotropic render matrices),
  enumerates embedded images and their rects, and detects already-bilevel images.
- **Pillow** does Floyd-Steinberg (`convert('1')`) and writes CCITT **G4** TIFF
  (`compression='group4'`).
- **numpy / OpenCV** handle Atkinson/clustered/ordered dithering, background
  flatten, despeckle (morphology), deskew (min-area-rect / projection), and
  thicken (dilation).
- **img2pdf** embeds the G4 TIFF into a PDF *without re-encoding* (the PDF carries
  a `CCITTFaxDecode` filter). **qpdf** linearizes the result.
- **Leptonica** (not installed here) is the production-grade choice for
  bilevel/MRC/despeckle if you move this off the current box; Ghostscript's
  `tiffg4` device + `-r204x196` is an alternative rasterizer when available.
