"""Render .pptx / .docx to PNG through the installed Office, for layout checks.

PowerPoint exports slides directly; Word goes through PDF and PyMuPDF. Both
are opened read-only, and an Office instance the user already has open is
left running.
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz
import pythoncom
import win32com.client


def pptx_to_png(pptx: Path, out_dir: Path, width: int = 1920, prefix: str = "slide") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}-*.png"):
        old.unlink()
    pythoncom.CoInitialize()
    app = win32com.client.Dispatch("PowerPoint.Application")
    had_open = app.Presentations.Count
    pres = app.Presentations.Open(str(pptx), ReadOnly=True, Untitled=False, WithWindow=False)
    try:
        height = round(width * pres.PageSetup.SlideHeight / pres.PageSetup.SlideWidth)
        paths = []
        for i, slide in enumerate(pres.Slides, start=1):
            target = out_dir / f"{prefix}-{i:02d}.png"
            slide.Export(str(target), "PNG", width, height)
            paths.append(target)
        return paths
    finally:
        pres.Close()
        if had_open == 0:
            app.Quit()


def docx_to_png(docx: Path, out_dir: Path, dpi: int = 90, prefix: str = "page") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}-*.png"):
        old.unlink()
    pdf = out_dir / f"{docx.stem}.pdf"
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx("Word.Application")
    app.Visible = False
    app.DisplayAlerts = 0
    try:
        doc = app.Documents.Open(str(docx), ReadOnly=True, AddToRecentFiles=False)
        doc.ExportAsFixedFormat(str(pdf), 17)  # wdExportFormatPDF
        doc.Close(False)
    finally:
        app.Quit()
    paths = []
    with fitz.open(pdf) as book:
        for i, page in enumerate(book, start=1):
            target = out_dir / f"{prefix}-{i:02d}.png"
            page.get_pixmap(dpi=dpi).save(target)
            paths.append(target)
    return paths


if __name__ == "__main__":
    src = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    fn = pptx_to_png if src.suffix.lower() == ".pptx" else docx_to_png
    for p in fn(src, out):
        print(p)
