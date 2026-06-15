#!/usr/bin/env python3
"""Check (and pip-install) the dependencies the optimizer needs.

Run this once before using the skill. Pip packages are installed with
--break-system-packages because the sandbox uses the system interpreter.
CLI tools (qpdf, optional ghostscript) can't be pip-installed; we just report
their presence so the caller knows what's available.
"""
import importlib
import shutil
import subprocess
import sys

PIP_PKGS = {
    "fitz": "pymupdf",
    "PIL": "Pillow",
    "numpy": "numpy",
    "cv2": "opencv-python-headless",
    "img2pdf": "img2pdf",
}


def ensure_pip(mod, pkg):
    try:
        importlib.import_module(mod)
        return True, "present"
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages",
             "-q", pkg],
            check=False,
        )
        try:
            importlib.import_module(mod)
            return True, "installed"
        except ImportError:
            return False, "FAILED to install"


def main():
    ok = True
    print("Python packages:")
    for mod, pkg in PIP_PKGS.items():
        good, status = ensure_pip(mod, pkg)
        ok = ok and good
        print(f"  {pkg:24s} {status}")

    print("CLI tools:")
    for tool, required in (("qpdf", True), ("gs", False)):
        path = shutil.which(tool)
        label = "required" if required else "optional"
        if path:
            print(f"  {tool:24s} present ({label})")
        else:
            print(f"  {tool:24s} MISSING ({label})")
            if required:
                ok = False

    if not ok:
        print("\nSome required dependencies are missing. qpdf must be installed "
              "via the system package manager; pip packages should self-install "
              "above. If a pip install failed, check network/proxy settings.")
        sys.exit(1)
    print("\nAll required dependencies satisfied.")


if __name__ == "__main__":
    main()
