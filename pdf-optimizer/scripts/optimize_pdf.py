#!/usr/bin/env python3
"""PDF Optimizer — channel-aware PDF optimization.

Two families of behavior:

  --mode size           shrink for email/web (downsample + re-encode + qpdf)
  --mode size-lossless  qpdf structural/stream optimization + linearize only
  --mode fax            convert to fax-native 1-bit CCITT-G4 PDF/TIFF

See SKILL.md and references/ for the why behind each knob. Flags override any
values from --config.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import fitz  # PyMuPDF
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fax_pipeline as fax  # noqa: E402


# --------------------------------------------------------------------------- #
# size mode                                                                   #
# --------------------------------------------------------------------------- #
def effective_dpi(page, xref, pix_w, pix_h):
    """Approximate the on-page rendered dpi of an image."""
    try:
        rects = page.get_image_rects(xref)
    except Exception:
        rects = []
    if not rects:
        return None
    r = rects[0]
    win = max(r.width, 1) / 72.0
    hin = max(r.height, 1) / 72.0
    return max(pix_w / win, pix_h / hin)


def optimize_size(in_pdf, out_pdf, target_dpi, jpeg_quality, linearize,
                  skip_below_dpi):
    doc = fitz.open(in_pdf)
    reencoded = 0
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                info = doc.extract_image(xref)
            except Exception:
                continue
            if info.get("bpc") == 1:
                continue  # already bilevel; leave to qpdf
            try:
                pil = Image.open(io_bytes(info["image"]))
            except Exception:
                continue
            edpi = effective_dpi(page, xref, pil.width, pil.height)
            if edpi and skip_below_dpi and edpi <= target_dpi * 1.1:
                continue
            scale = (target_dpi / edpi) if edpi and edpi > target_dpi else 1.0
            new_w = max(1, int(pil.width * scale))
            new_h = max(1, int(pil.height * scale))
            if scale < 1.0:
                pil = pil.resize((new_w, new_h), Image.LANCZOS)
            buf = io_buffer()
            pil.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality,
                                    optimize=True)
            try:
                doc.update_stream(xref, buf.getvalue(), new=False)
                # re-declare as DCT/JPEG
                doc.xref_set_key(xref, "Filter", "/DCTDecode")
                doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")
                doc.xref_set_key(xref, "BitsPerComponent", "8")
                doc.xref_set_key(xref, "Width", str(new_w))
                doc.xref_set_key(xref, "Height", str(new_h))
                reencoded += 1
            except Exception:
                continue

    tmp = tempfile.mktemp(suffix=".pdf")
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    _qpdf(tmp, out_pdf, linearize)
    os.unlink(tmp)
    return {
        "mode": "size",
        "input": in_pdf, "output": out_pdf,
        "input_bytes": os.path.getsize(in_pdf),
        "output_bytes": os.path.getsize(out_pdf),
        "images_reencoded": reencoded,
    }


def optimize_size_lossless(in_pdf, out_pdf, linearize):
    _qpdf(in_pdf, out_pdf, linearize, lossless=True)
    return {
        "mode": "size-lossless",
        "input": in_pdf, "output": out_pdf,
        "input_bytes": os.path.getsize(in_pdf),
        "output_bytes": os.path.getsize(out_pdf),
    }


def _qpdf(src, dst, linearize, lossless=False):
    if not shutil.which("qpdf"):
        # qpdf missing: fall back to a plain copy so we still produce output
        shutil.copy(src, dst)
        return
    cmd = ["qpdf", src, dst, "--object-streams=generate",
           "--compress-streams=y", "--recompress-flate"]
    if linearize:
        cmd.append("--linearize")
    r = subprocess.run(cmd, capture_output=True)
    # qpdf returns 3 for warnings (still writes output); only treat >3 as fatal
    if r.returncode not in (0, 3) or not os.path.exists(dst):
        shutil.copy(src, dst)


# small lazy io helpers (avoid top-level import noise)
def io_bytes(b):
    import io
    return io.BytesIO(b)


def io_buffer():
    import io
    return io.BytesIO()


# --------------------------------------------------------------------------- #
# config + CLI                                                                #
# --------------------------------------------------------------------------- #
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
        resolution=pick(args.fax_resolution, "resolution", "fine"),
        dither=pick(args.dither, "dither", "auto"),
        fax_heavy=args.fax_heavy or fc.get("fax_heavy", False),
        segmentation=pick(args.segmentation, "segmentation", "embedded"),
        thicken=args.thicken or fc.get("thicken", False),
        flatten_bg=pick(args.flatten_bg, "flatten_bg", True),
        despeckle=pick(args.despeckle, "despeckle", True),
        deskew=pick(args.deskew, "deskew", True),
        fmt=pick(args.format, "format", "pdf"),
        line_rate_bps=pick(args.line_rate, "line_rate_bps", 14400),
    )


def main():
    p = argparse.ArgumentParser(description="Channel-aware PDF optimizer")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--mode", choices=["size", "size-lossless", "fax"],
                   default=None)
    p.add_argument("--config")
    p.add_argument("--report")
    # size
    p.add_argument("--target-dpi", type=int, default=None)
    p.add_argument("--jpeg-quality", type=int, default=None)
    p.add_argument("--linearize", action="store_true", default=None)
    # fax
    p.add_argument("--fax-resolution",
                   choices=["standard", "fine", "superfine"], default=None)
    p.add_argument("--dither",
                   choices=["auto", "floyd", "atkinson", "ordered",
                            "clustered", "none"], default=None)
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
    p.add_argument("--preview-page", type=int, default=None)
    args = p.parse_args()

    cfg = load_config(args.config)
    mode = args.mode or cfg.get("mode", "size")

    if mode == "fax":
        opt = build_fax_options(cfg, args)
        if args.preview_page:
            png = os.path.splitext(args.output)[0] + f".preview_p{args.preview_page}.png"
            fax.render_preview(args.input, args.preview_page, png, opt)
            print(f"Preview written: {png}")
        report = fax.convert_pdf(args.input, args.output, opt)
    elif mode == "size-lossless":
        lin = args.linearize if args.linearize is not None else \
            cfg.get("size", {}).get("linearize", True)
        report = optimize_size_lossless(args.input, args.output, lin)
    else:  # size
        sc = cfg.get("size", {})
        report = optimize_size(
            args.input, args.output,
            target_dpi=args.target_dpi or sc.get("target_dpi", 150),
            jpeg_quality=args.jpeg_quality or sc.get("jpeg_quality", 75),
            linearize=(args.linearize if args.linearize is not None
                       else sc.get("linearize", True)),
            skip_below_dpi=sc.get("skip_below_dpi", True),
        )

    report_path = args.report or cfg.get("report")
    if report_path:
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

    _print_summary(report)


def _print_summary(report):
    ib, ob = report["input_bytes"], report["output_bytes"]
    if ib:
        change = (ob - ib) / ib * 100
        if change <= 0:
            size_note = f"{abs(change):.1f}% smaller"
        else:
            size_note = f"{change:.1f}% larger"
    else:
        size_note = "n/a"
    print(f"mode: {report['mode']}")
    print(f"input:  {ib:,} bytes")
    print(f"output: {ob:,} bytes  ({size_note})")
    if report["mode"] == "fax":
        print(f"pages:  {len(report['pages'])}")
        print(f"est. transmission: {report['total_est_transmission_s']:.0f}s "
              f"(~{report['total_est_transmission_s']/60:.1f} min)")
        if report["warnings"]:
            print("warnings: " + ", ".join(report["warnings"]))
    elif report["mode"] == "size":
        print(f"images re-encoded: {report.get('images_reencoded', 0)}")


if __name__ == "__main__":
    main()
