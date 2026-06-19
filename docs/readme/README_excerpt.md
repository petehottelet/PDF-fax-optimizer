## Visual gallery — Prestige Estates fax cover sheet

The screenshots below are all generated end-to-end through the real pipeline
by [`00_Project_Files/_run_readme_demo.py`](../../00_Project_Files/_run_readme_demo.py)
on `Prestige_Estates_v3.docx`. Re-run that script to refresh them after a
pipeline change.

### Halftone style comparison

Every screen in the `SCREENS` registry, applied to the same letter cover sheet.
The three reference panels (Original / Grayscale / Standard fax) lead the
grid so the halftone choice is always anchored to the source. `floyd`,
`jarvis`, and `edd` are highlighted as the **optimal solutions** for a
forms-and-photo page like this one: they preserve fine document text, hold
photographic detail in the masthead photo, and keep edge sharpness on the
billboard logo.

<p align="center">
  <img src="docs/readme/halftone_grid.png" alt="Halftone style contact sheet — every screen in the registry, with floyd / jarvis / edd marked OPTIMAL" width="100%">
</p>

### Text rescue features

`preserve_text` (default-on) whitens colored "highlight pill" backgrounds so
the dark text on top reads cleanly through the 1-bit threshold. `recover_text`
(default-off; opt-in with `--recover-text on`) uses OCR to find text inside
halftoned image regions and recolors it BLACK or WHITE by the **#808080 rule**,
compositing the recolored glyphs ABOVE the halftone layer.

<p align="center">
  <img src="docs/readme/text_rescue.png" alt="Text rescue features — preserve_text and recover_text before/after" width="100%">
</p>

### Other built-in features

The 4-panel `--sample N` diagnostic gives you a *single image* that lets you
see the original, a true grayscale of it, the standard fax baseline, and the
optimized output side-by-side — confirm legibility before sending. The bottom
row shows the auto-picker's reasoning: a naive Otsu fax loses every photo, a
Floyd ED optimized pass keeps text + photo, and `--fax-heavy` falls back to a
compression-priority clustered screen when bytes-on-the-wire matter more than
photo detail.

<p align="center">
  <img src="docs/readme/features.png" alt="Other built-in features — 4-panel sample, basic vs optimal vs fax-heavy" width="100%">
</p>
