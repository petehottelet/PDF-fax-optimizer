# Config Schema

Pass `--config config.json` instead of (or alongside) flags. Any flag given on
the command line overrides the matching config value. All keys are optional;
omitted keys fall back to the documented defaults.

## Schema

```jsonc
{
  "fax": {
    "resolution": "superfine",    // "standard" | "fine" | "superfine" (DEFAULT)
                                  //   raster pages keep their NATIVE pixel grid;
                                  //   the preset only sets the halftone screen
                                  //   detail and the rasterization DPI for vector
                                  //   pages.
    "transmission_safe": false,   // clamp the final scanline to 1728 px so a real
                                  //   Group-3 fax can transmit it. Default OFF
                                  //   prefers legibility (native resolution) over
                                  //   strict G3 transmissibility.

    // ---- photo halftone schema (continuous-tone regions only) ----
    "dither": "auto",             // "auto" | "clustered" | "green-noise"
                                  //   | "blue-noise" | "atkinson" | "floyd"
                                  //   | "line" (aka "woodcut"/"engraving")
                                  //   | "ordered" | "edd" | "jarvis" | "stucki"
                                  //   | "sierra" | "none"
    "green_noise_coarseness": 4.0, // green-noise AM<->FM knob ~2 (detail)..8 (robust)
    "tone_curve": "auto",         // per-family dot-gain pre-correction | "none"
    "sharpen": false,             // edge-aware unsharp on photos before halftoning
    "fax_heavy": false,           // bias toward clustered (compresses, robust)

    // ---- text / line-art binarization (never halftoned) ----
    "text_binarize": "contrast",  // "contrast" (force gray/light text to solid
                                  //   black) | "sauvola" | "niblack" | "wolf"
                                  //   | "bradley" | "otsu"
    "text_in_image": true,        // fallback rescue: where stroke geometry inside
                                  //   the photo region looks like a text line, keep
                                  //   the binarized value instead of the halftone.
                                  //   The OCR-driven robust_image_text below is the
                                  //   primary path; this is a safety net.
    "ocr_text": "auto",           // OCR-driven #808080 polarity for text OUTSIDE
                                  //   images (page header/footer/form text). "auto"
                                  //   runs when an OCR engine is available; "off"
                                  //   falls back to the binarizer's default
                                  //   black-on-white. Needs rapidocr-onnxruntime.
    "robust_image_text": "auto",  // OCR-driven #808080 polarity for text INSIDE
                                  //   images (signage, captions baked into a
                                  //   photo). Same engine as ocr_text; recoloured
                                  //   glyphs ride a layer composited ABOVE the
                                  //   halftone so the screen never disturbs them.
                                  //   "off" disables only the within-image pass.
    "ocr_conf_min": 0.5,          // minimum OCR confidence to recolour a word
    "robust_text_stroke": 0.15,   // (deprecated, retained for back-compat)
    "segmentation": "embedded",   // "embedded" (use PDF image rects)
                                  //   | "variance" (heuristic for flat scans)
                                  //   | "none" (whole page one strategy)
    "thicken": false,             // dilate thin strokes/small fonts so they survive
    "flatten_bg": true,           // push near-white to pure white before threshold
    "despeckle": true,            // remove isolated black pixels
    "deskew": true,               // straighten skewed scans
    "format": "pdf",              // "pdf" (CCITT G4 in PDF) | "tiff" (Class-F G4)
    "line_rate_bps": 14400,       // basis for transmission-time estimate
    "page_overhead_s": 1.5        // per-page fax handshake/overhead estimate
  },

  "report": "OUTPUT.report.json", // write machine-readable report here
  "preview_page": null            // 1-based page number to render as PNG, or null
}
```

> Note: `--compare-page N`, `--compare-methods a,b,c`, `--sample N`, and
> `--robust-text-preview N` are CLI-only flags, not config keys.

## Annotated example — faxing scanned clinical paperwork with a photo

```jsonc
{
  "fax": {
    "resolution": "superfine",    // small print on intake forms needs the dpi
    "dither": "atkinson",         // one embedded photo, want detail, line is clean
    "segmentation": "embedded",   // forms are vector+image: route the photo only
    "thicken": true,              // faint signatures and small fonts
    "flatten_bg": true,           // scanner gave a gray cast; force white
    "despeckle": true,
    "deskew": true,
    "format": "pdf",
    "line_rate_bps": 14400
  },
  "report": "intake.report.json",
  "preview_page": 1               // eyeball page 1 before sending
}
```

## Report output shape

The `report` file (and `--report`) is written as JSON:

```jsonc
{
  "mode": "fax",
  "input": "intake.pdf",
  "output": "intake.fax.pdf",
  // (input_bytes/output_bytes are absolute file sizes, not a compression goal)
  "input_bytes": 4210233,
  "output_bytes": 188440,
  "pages": [
    {
      "index": 1,
      "encoded_bytes": 61220,
      "est_transmission_s": 35.5,
      "photo_regions": 1,
      "photo_fraction": 0.19,
      "dither": "green-noise",
      "text_binarize": "contrast",
      "already_bilevel": false,
      "ocr_text": {                 // text OUTSIDE images (header/footer/form text)
        "scope": "doc",
        "engine": "rapidocr-onnxruntime",
        "words_recognized": 27,
        "words_recolored_black": 25,
        "words_recolored_white": 0,
        "words": [ { "text": "FAX", "conf": 0.99, "polarity": "black",
                     "field_gray": 250.0, "wcag_contrast": 18.6,
                     "bbox": [x0, y0, x1, y1] } ]
      },
      "robust_text": {              // text INSIDE images (signage, captions)
        "scope": "image",
        "engine": "rapidocr-onnxruntime",
        "words_recognized": 2,
        "words_recolored_black": 2, // #808080 rule: field >= 128 -> BLACK
        "words_recolored_white": 0, // #808080 rule: field < 128 -> WHITE
        "words": [ { "text": "VILLA DEL SOL", "conf": 0.98,
                     "polarity": "black", "field_gray": 142.0,
                     "wcag_contrast": 2.2, "bbox": [x0, y0, x1, y1] } ]
      },
      "warnings": ["small_text_below_min", "image_text:recolored:2",
                   "doc_text:recolored:25"]
    }
  ],
  "total_est_transmission_s": 142.0,
  "warnings": []
}
```

Always surface `total_est_transmission_s`, page count, and any `warnings` to the
user. Long per-page transmission times and `inverted_or_heavy_black` are the
signals to revisit knobs before sending. `doc_text:recolored:*` and
`image_text:recolored:*` count words the OCR-driven #808080 rule recoloured;
per-word detail (text, polarity, field luminance, OCR confidence) lives under
`pages[].ocr_text.words` and `pages[].robust_text.words` respectively. Surface
these word lists to the user so they can verify OCR didn't misread.
