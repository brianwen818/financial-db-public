"""Tile rendered pages into review sheets: contact_sheet.py <dir> <prefix> [cols] [rows]."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def sheets(folder: Path, prefix: str, cols: int = 2, rows: int = 2, width: int = 2000) -> list[Path]:
    pages = sorted(folder.glob(f"{prefix}-*.png"))
    for old in folder.glob(f"sheet-{prefix}-*.png"):
        old.unlink()
    out = []
    per = cols * rows
    for start in range(0, len(pages), per):
        images = [Image.open(p).convert("RGB") for p in pages[start:start + per]]
        cell_w = width // cols
        cell_h = round(cell_w * images[0].height / images[0].width)
        sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), (120, 120, 120))
        for i, image in enumerate(images):
            tile = image.resize((cell_w - 8, cell_h - 8))
            sheet.paste(tile, ((i % cols) * cell_w + 4, (i // cols) * cell_h + 4))
        target = folder / f"sheet-{prefix}-{start // per + 1:02d}.png"
        sheet.save(target)
        out.append(target)
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    for path in sheets(Path(args[0]), args[1], *(int(a) for a in args[2:])):
        print(path)
