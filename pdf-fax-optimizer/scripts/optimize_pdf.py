#!/usr/bin/env python3
"""PDF fax optimizer — convert a PDF into a fax-ready 1-bit CCITT-G4 PDF/TIFF.

The whole job is a document that arrives LEGIBLE over a Group-3 fax line. See
SKILL.md and references/ for the why behind each knob. Flags override any values
from --config.

Need to shrink a PDF for email/web instead? Use the companion skill:
https://github.com/petehottelet/pdf-email-optimizer
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fax_pipeline as fax  # noqa: E402
import fitz  # noqa: E402  # for the deck-page-count line of the progress print


def load_config(path):
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


def build_fax_options(cfg, args) -> fax.FaxOptions:
    fc = cfg.get("fax", {})

    def pick(flag, key, default):
        return flag if flag is not None else fc.get(key, default)

    return fax.FaxOptions(
        resolution=pick(args.fax_resolution, "resolution", fax.DEFAULT_RESOLUTION),
        dither=pick(args.dither, "dither", "auto"),
        fax_heavy=args.fax_heavy or fc.get("fax_heavy", False),
        segmentation=pick(args.segmentation, "segmentation", "embedded"),
        thicken=args.thicken or fc.get("thicken", False),
        flatten_bg=pick(args.flatten_bg, "flatten_bg", True),
        despeckle=pick(args.despeckle, "despeckle", True),
        deskew=pick(args.deskew, "deskew", True),
        fmt=pick(args.format, "format", "pdf"),
        line_rate_bps=pick(args.line_rate, "line_rate_bps", 14400),
        text_binarize=pick(args.text_binarize, "text_binarize", "contrast"),
        tone_curve=pick(args.tone_curve, "tone_curve", "auto"),
        sharpen=args.sharpen or fc.get("sharpen", False),
        green_noise_coarseness=pick(args.green_noise_coarseness,
                                    "green_noise_coarseness", 4.0),
        text_in_image=pick(args.text_in_image, "text_in_image", True),
        preserve_text=pick(args.preserve_text, "preserve_text", True),
        recover_text=pick(args.recover_text, "recover_text", "off"),
        ocr_text=pick(args.ocr_text, "ocr_text", "auto"),
        ocr_conf_min=pick(args.ocr_conf, "ocr_conf_min", 0.5),
        transmission_safe=(args.transmission_safe
                           or fc.get("transmission_safe", False)),
        basic=(args.basic or fc.get("basic", False)),
        dot_shape=pick(args.dot_shape, "dot_shape", "round"),
        screen_angle=pick(args.screen_angle, "screen_angle", 0.0),
        hatch_angles=tuple(_parse_angles(
            pick(args.hatch_angles, "hatch_angles", "0,90"))),
    )


def _parse_angles(spec) -> tuple:
    """Accept either a comma-separated string ("0,45,90"), a list of floats,
    or a tuple — all collapse to a tuple of floats for hatch_angles."""
    if isinstance(spec, (tuple, list)):
        return tuple(float(a) for a in spec)
    return tuple(float(a.strip()) for a in str(spec).split(",") if a.strip())


def main():
    p = argparse.ArgumentParser(description="Convert a PDF to a fax-ready "
                                            "1-bit CCITT-G4 PDF/TIFF")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    # Kept for backward-compatible invocations; fax is the only mode.
    p.add_argument("--mode", choices=["fax"], default="fax")
    p.add_argument("--config")
    p.add_argument("--report")
    p.add_argument("--fax-resolution",
                   choices=["standard", "fine", "superfine"], default=None)
    p.add_argument("--dither",
                   choices=["auto", "none", "threshold", "ordered", "bayer",
                            "clustered", "round", "screen", "floyd", "atkinson",
                            "jarvis", "stucki", "sierra", "blue-noise",
                            "green-noise", "green", "blue", "stipple", "edd",
                            "line", "woodcut", "engraving", "lines",
                            "crosshatch", "hatch", "etching", "mezzotint",
                            "grain"],
                   default=None,
                   help="photo halftone schema (see references). New options: "
                        "`screen` (generalised AM dot with --dot-shape / "
                        "--screen-angle), `crosshatch` (pen-and-ink etching), "
                        "`mezzotint` (random-threshold grain; expressive — not "
                        "eligible for `auto`)")
    p.add_argument("--dot-shape",
                   choices=["round", "square", "diamond", "ellipse"],
                   default=None, dest="dot_shape",
                   help="spot function for `--dither screen`: round (default; "
                        "identical to `clustered`), square (crisp blocky), "
                        "diamond (newspaper-photo look), or ellipse (smoother "
                        "midtone joins)")
    p.add_argument("--screen-angle", type=float, default=None,
                   dest="screen_angle", metavar="DEG",
                   help="rotate `--dither screen` or `--dither line` by this "
                        "many degrees. 0 is fax-friendly (horizontal runs); "
                        "off-axis angles >|15°| emit a G4-cost warning")
    p.add_argument("--hatch-angles", default=None, dest="hatch_angles",
                   metavar="\"0,90\"",
                   help="comma-separated angle list for `--dither crosshatch` "
                        "(default \"0,90\"). Two perpendicular angles keep "
                        "transition density near `line`; >2 angles emit a "
                        "G4-cost warning")
    p.add_argument("--green-noise-coarseness", type=float, default=None,
                   dest="green_noise_coarseness",
                   help="green-noise AM<->FM knob ~2 (detail) .. 8 (robust)")
    p.add_argument("--text-binarize",
                   choices=["contrast", "sauvola", "niblack", "wolf", "bradley",
                            "otsu"],
                   default=None,
                   help="binarizer for text/line content; 'contrast' (default) "
                        "maximizes legibility by forcing gray/light text to "
                        "solid black")
    p.add_argument("--tone-curve", choices=["auto", "none"], default=None,
                   help="per-family dot-gain pre-correction for photos")
    p.add_argument("--sharpen", action="store_true",
                   help="edge-aware unsharp on photo regions before halftoning")
    p.add_argument("--no-text-in-image", dest="text_in_image",
                   action="store_false", default=None,
                   help="don't rescue text baked into photos; halftone the whole "
                        "image region (text-in-image rescue is on by default)")
    p.add_argument("--no-preserve-text",
                   dest="preserve_text",
                   action="store_false", default=None,
                   help="don't lift small saturated-colour fields containing "
                        "dark text to white before binarization. By default, "
                        "any colored fill that would lose contrast in "
                        "grayscale and shred the dark text on top — slide "
                        "highlight chips, dashboard status badges, colored "
                        "table cells, tinted callout boxes, filled banners — "
                        "gets rescued: the fill is flattened to white so the "
                        "binarizer sees crisp black-on-white and prints the "
                        "text legibly instead of mangling it. Disable with "
                        "this flag if you'd rather keep the fill as a "
                        "halftoned tone band")
    p.add_argument("--recover-text", choices=["auto", "on", "off"], default=None,
                   dest="recover_text",
                   help="rescue text baked INSIDE images (signage, captions): OCR "
                        "locates words; each word's ORIGINAL glyph pixels are "
                        "recoloured BLACK on a light/mid field or WHITE on a dark "
                        "field by the #808080 rule, and the recoloured glyphs ride "
                        "a layer composited ABOVE the halftone (so the screen can "
                        "never disturb them). **Off by default** — pass "
                        "`--recover-text on` (or `auto` to enable when an OCR engine "
                        "is available) to opt in")
    p.add_argument("--recover-text-stroke", type=float, default=None,
                   dest="recover_text_stroke", metavar="MULT",
                   help="(deprecated, retained for back-compat: glyphs now ride a "
                        "compositing layer so a stroke backing isn't needed)")
    p.add_argument("--recover-text-preview", type=int, default=None,
                   dest="recover_text_preview", metavar="PAGE",
                   help="(legacy) write a before/after contact sheet for PAGE "
                        "showing the page faxed WITHOUT vs WITH within-image "
                        "recover-text recolor. Equivalent to "
                        "--sample PAGE --sample-include no_recover,with_recover")
    p.add_argument("--sample", type=int, default=None, metavar="PAGE",
                   help="write a labeled contact sheet for PAGE so the user can "
                        "confirm the output is legible before sending. Default "
                        "is a 4-panel sheet (original / grayscale / default-fax "
                        "Otsu / auto-recommended). Use --panels to ask for more "
                        "(or fewer); the sheet carries a settings header that "
                        "documents which options produced it")
    p.add_argument("--panels", default=None, metavar="K",
                   help="how many panels --sample emits: one of "
                        "1, 2, 4 (default), 6, 8, 12, 20, or 'max' (=20). "
                        "Higher counts add more halftone screens for "
                        "side-by-side comparison; 20 is the full SCREENS "
                        "catalogue (every dither in the registry) on YOUR "
                        "input page")
    p.add_argument("--sample-include", default=None, metavar="LIST",
                   help="comma-separated list of panel content keys for a "
                        "custom --sample recipe (overrides --panels). Keys: "
                        "orig, gray, default_fax, optimized, recommended, or "
                        "any dither name including screen-{square,diamond,"
                        "ellipse}. Example: orig,gray,clustered,floyd,line")
    p.add_argument("--no-sample-header", dest="sample_header",
                   action="store_false", default=True,
                   help="omit the 3-line settings-header strip at the top "
                        "of --sample output (default: header is shown)")
    p.add_argument("--ocr-text", choices=["off", "auto", "on"], default=None,
                   dest="ocr_text",
                   help="Within the OCR pass, control whether OUTSIDE-image "
                        "text (header/footer/form) is also recoloured by the "
                        "#808080 rule. Only meaningful when --recover-text is "
                        "on; the OCR engine is shared and ONLY runs at all if "
                        "--recover-text is on (it is the slow step — 20+ min on "
                        "a 6-page 391-DPI deck — and the chroma-aware photo "
                        "segmenter plus the adaptive binarizer route doc text "
                        "correctly without it). Needs rapidocr-onnxruntime")
    p.add_argument("--ocr-conf", type=float, default=None, dest="ocr_conf",
                   metavar="C",
                   help="minimum OCR confidence (0–1) to recolour a word "
                        "(default 0.5)")
    p.add_argument("--basic", action="store_true",
                   help="bare-minimum pipeline: render to grayscale at the "
                        "source's effective DPI and Otsu-threshold to 1-bit. "
                        "No MRC photo segmentation, no OCR, no halftoning, no "
                        "flatten_bg / despeckle / deskew / thicken — just the "
                        "minimum steps to land a 1-bit page in a G4 frame. "
                        "Use as a baseline reference, or when the full "
                        "pipeline's enhancements are unwanted (e.g. for "
                        "synthetic / already-flat documents). "
                        "--transmission-safe still applies")
    p.add_argument("--transmission-safe", action="store_true",
                   help="clamp the rendered scanline to 1728 px so a real Group-3 "
                        "fax machine can transmit the file. Without this, the "
                        "output is rendered at the source's native resolution "
                        "(best legibility) but may exceed standard fax widths")
    p.add_argument("--fax-heavy", action="store_true")
    p.add_argument("--segmentation",
                   choices=["embedded", "variance", "none"], default=None)
    p.add_argument("--thicken", action="store_true")
    p.add_argument("--no-flatten-bg", dest="flatten_bg", action="store_false",
                   default=None)
    p.add_argument("--no-despeckle", dest="despeckle", action="store_false",
                   default=None)
    p.add_argument("--no-deskew", dest="deskew", action="store_false",
                   default=None)
    p.add_argument("--format", choices=["pdf", "tiff"], default=None)
    p.add_argument("--line-rate", type=int, default=None)
    p.add_argument("--preview-page", type=int, default=None,
                   help="(legacy) write a bare grayscale PNG of the bilevel "
                        "output for PAGE \u2014 no labels, no header. Use "
                        "--sample PAGE --panels 1 for a labelled version")
    p.add_argument("--compare-page", type=int, default=None,
                   help="(legacy) render this page through several halftone "
                        "methods into one labeled contact sheet. Equivalent to "
                        "--sample PAGE --panels 6 (--panels 8 with "
                        "--compare-original)")
    p.add_argument("--compare-methods", default=None,
                   help="(legacy) comma-separated halftone names for "
                        "--compare-page (default: the curated 6-up set). Use "
                        "--sample-include for a custom recipe with the new "
                        "unified --sample flag")
    p.add_argument("--compare-original", action="store_true",
                   help="(legacy) lead the contact sheet with original-color "
                        "(#1) and true-grayscale (#2) references, then four "
                        "halftones (6-up). Equivalent to "
                        "--sample PAGE --sample-include "
                        "orig,gray,clustered,green-noise,blue-noise,line")
    p.add_argument("--keep-converted-pdf", action="store_true",
                   help="when input is Office/image, keep the intermediate PDF "
                        "next to the output instead of deleting it")
    # ---- optional: send the optimized file via a cloud fax API ----
    s = p.add_argument_group("sending (optional)")
    s.add_argument("--send", choices=["mfax", "phaxio", "generic"], default=None,
                   help="after optimizing, transmit the output via this fax API")
    s.add_argument("--to", help="recipient fax number in E.164 (with --send)")
    s.add_argument("--api-key")
    s.add_argument("--api-secret")
    s.add_argument("--base-url")
    s.add_argument("--dry-run", action="store_true",
                   help="with --send: print the request instead of transmitting")
    s.add_argument("--cover-page", action="store_true")
    s.add_argument("--recipient-name")
    s.add_argument("--subject")
    s.add_argument("--notes")
    s.add_argument("--caller-id")
    s.add_argument("--url")
    s.add_argument("--auth-header")
    s.add_argument("--basic-user")
    s.add_argument("--basic-pass")
    s.add_argument("--to-field", default="to")
    s.add_argument("--file-field", default="file")
    s.add_argument("--field", action="append")
    args = p.parse_args()

    cfg = load_config(args.config)
    opt = build_fax_options(cfg, args)

    # Accept Word/PowerPoint/Excel/OpenDocument/text and loose images by
    # normalizing them to PDF first; the rest of the pipeline is unchanged.
    import to_pdf
    keep_dir = os.path.dirname(os.path.abspath(args.output)) \
        if args.keep_converted_pdf else None
    try:
        src_pdf, converted_is_temp = to_pdf.ensure_pdf(args.input, keep_dir)
    except RuntimeError as e:
        sys.exit(str(e))
    if src_pdf != args.input:
        print(f"converted {args.input} -> {src_pdf}")

    comparison = None
    if args.compare_page:
        methods = ([m.strip() for m in args.compare_methods.split(",")]
                   if args.compare_methods else None)
        png = (os.path.splitext(args.output)[0]
               + f".compare_p{args.compare_page}.png")
        comparison = fax.render_comparison(
            src_pdf, args.compare_page, png, opt, methods,
            include_original=args.compare_original)
        print(f"Comparison contact sheet: {png}")
        print(f"  recommended: {comparison['recommended']}  "
              f"(smallest: {comparison['smallest']})")
        print(f"  why: {comparison['reason']}")
        print("  spend your eye tokens \u2014 per-method G4 size / page:")
        for m, mm in comparison["methods"].items():
            if mm.get("original"):
                print(f"    {m:11s} {'  source':>9s}  "
                      f"({mm.get('note', 'reference, not faxed')})")
                continue
            star = "  <- recommended" if m == comparison["recommended"] else ""
            print(f"    {m:11s} {mm['encoded_bytes'] / 1024:6.0f} KB  "
                  f"~{mm['est_transmission_s']:.0f}s{star}")

    if args.preview_page:
        png = os.path.splitext(args.output)[0] + f".preview_p{args.preview_page}.png"
        fax.render_preview(src_pdf, args.preview_page, png, opt)
        print(f"Preview written: {png}")

    if args.recover_text_preview:
        png = (os.path.splitext(args.output)[0]
               + f".recovertext_p{args.recover_text_preview}.png")
        rtp = fax.render_recover_text_preview(
            src_pdf, args.recover_text_preview, png, opt)
        rt = rtp.get("recover_text") or {}
        print(f"Recover-text before/after: {png}")
        print(f"  words recognised: {rt.get('words_recognized', 0)}, "
              f"recoloured black: {rt.get('words_recolored_black', 0)}, "
              f"recoloured white: {rt.get('words_recolored_white', 0)}")

    if args.sample:
        # Resolve --panels: missing = 4 (default); "max" alias = 20; else
        # parse as int and validate against PANEL_RECIPES.
        valid_counts = sorted(fax.PANEL_RECIPES.keys())
        if args.sample_include:
            include = [k.strip() for k in args.sample_include.split(",")
                       if k.strip()]
            panel_count = len(include)
        else:
            include = None
            raw = args.panels
            if raw is None:
                panel_count = 4
            elif str(raw).lower() in ("max", "all"):
                panel_count = max(valid_counts)
            else:
                try:
                    panel_count = int(raw)
                except (TypeError, ValueError):
                    raise SystemExit(
                        f"--panels: invalid value {raw!r}; expected one of "
                        f"{valid_counts + ['max']}")
                if panel_count not in valid_counts:
                    raise SystemExit(
                        f"--panels {panel_count}: not one of {valid_counts} "
                        "(use --sample-include for a custom recipe)")
        png = os.path.splitext(args.output)[0] + f".sample_p{args.sample}.png"
        spl = fax.render_contact_sheet(
            src_pdf, args.sample, png, opt,
            panel_count=panel_count, include=include,
            show_header=getattr(args, "sample_header", True))
        n = spl.get("panel_count", panel_count)
        rec = spl.get("recommended")
        reason = spl.get("reason") or ""
        smallest = spl.get("smallest")
        print(f"{n}-panel contact sheet: {png}")
        if rec:
            print(f"  auto-pick: {rec}" + (f" \u2014 {reason}" if reason else ""))
        if smallest and smallest != rec:
            sm = spl["panels"].get(smallest, {})
            kb = sm.get("encoded_bytes", 0) / 1024
            print(f"  smallest G4: {smallest} ({kb:.0f} KB)")

    def _progress(i, n, rep):
        # One line per page, flushed immediately, so a long deck doesn't go
        # silent for minutes. The size + dither + transmission read tells the
        # user the page actually came out (and roughly how big it'll be);
        # the page count anchors it within the deck.
        kb = rep.encoded_bytes / 1024
        print(f"  page {i}/{n}: {kb:6.1f} KB  dither={rep.dither}  "
              f"~{rep.est_transmission_s:.0f}s tx", flush=True)

    print(f"converting {src_pdf} ({fitz.open(src_pdf).page_count} pages)...",
          flush=True)
    report = fax.convert_pdf(src_pdf, args.output, opt, progress=_progress)
    # Report against the original file the user handed us, not the intermediate.
    report["input"] = args.input
    report["input_bytes"] = os.path.getsize(args.input)
    if src_pdf != args.input:
        report["converted_pdf"] = src_pdf
    if comparison:
        report["comparison"] = comparison

    if converted_is_temp and src_pdf != args.input:
        try:
            import shutil
            shutil.rmtree(os.path.dirname(src_pdf), ignore_errors=True)
        except Exception:
            pass

    report_path = args.report or cfg.get("report")
    if report_path:
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

    _print_summary(report)

    if args.send:
        if not args.to:
            sys.exit("--send requires --to (recipient fax number in E.164)")
        import send_fax
        print(f"\nsending {args.output} via {args.send} to {args.to}"
              + (" (dry run)" if args.dry_run else "") + " ...")
        result = send_fax.send(args.send, args.output, args.to, args)
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            sys.exit(1)


def _print_summary(report):
    ib, ob = report["input_bytes"], report["output_bytes"]
    if ib:
        change = (ob - ib) / ib * 100
        size_note = (f"{abs(change):.1f}% smaller" if change <= 0
                     else f"{change:.1f}% larger")
    else:
        size_note = "n/a"
    print(f"mode: {report['mode']}")
    print(f"input:  {ib:,} bytes")
    print(f"output: {ob:,} bytes  ({size_note})")
    print(f"pages:  {len(report['pages'])}")
    dithers = sorted({p.get("dither", "") for p in report["pages"]} - {""})
    if dithers:
        print(f"halftone used: {', '.join(dithers)}")
    binar = sorted({p.get("text_binarize", "") for p in report["pages"]} - {""})
    if binar:
        print(f"text binarizer: {', '.join(binar)}")
    def _summarise(words, label_scope):
        if not words:
            return
        rendered = [w for w in words if w.get("rendered", True)]
        nb_rend = sum(1 for w in rendered if w.get("polarity") == "black")
        nw_rend = sum(1 for w in rendered if w.get("polarity") == "white")
        if not rendered:
            print(f"{label_scope} text: {len(words)} word(s) read; binarizer "
                  "rendering preserved (no recolour needed)")
            return
        shown = ", ".join(f"\u201c{w['text']}\u201d({w['polarity'][0]})"
                          for w in rendered[:8])
        more = "" if len(rendered) <= 8 else f", +{len(rendered) - 8} more"
        skipped = len(words) - len(rendered)
        skip_note = f" ({skipped} on white field left to binarizer)" if skipped else ""
        print(f"{label_scope} text recoloured (#808080 rule): {nb_rend} black + "
              f"{nw_rend} white{skip_note} — {shown}{more}")

    _summarise([w for p in report["pages"]
                for w in (p.get("recover_text") or {}).get("words", [])], "image")
    _summarise([w for p in report["pages"]
                for w in (p.get("ocr_text") or {}).get("words", [])], "document")
    print(f"est. transmission: {report['total_est_transmission_s']:.0f}s "
          f"(~{report['total_est_transmission_s'] / 60:.1f} min)")
    if report.get("comparison"):
        print(f"comparison sheet: {report['comparison']['output']}")
    if report["warnings"]:
        print("warnings: " + ", ".join(report["warnings"]))


if __name__ == "__main__":
    main()
