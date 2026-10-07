"""Filesystem primitives that tolerate Windows' transient locks.

``os.replace`` and ``os.rename`` are atomic on Windows, but they fail outright
with ``PermissionError`` (WinError 5) whenever another process holds a handle on
the source or target — a real-time antivirus scan of a just-written file, the
shell indexer, or in this pipeline's case Excel itself, which keeps a handle on
a workbook for a moment after ``Close()`` returns. The lock is short-lived, so a
bounded backoff clears it.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 12
_MAX_DELAY = 2.0


def _retry(op, src: Path, dst: Path, attempts: int, what: str) -> None:
    delay = 0.05
    for attempt in range(1, attempts + 1):
        try:
            op(src, dst)
            if attempt > 1:
                log.info("%s %s -> %s succeeded on attempt %d", what, src.name, dst.name, attempt)
            return
        except PermissionError:
            if attempt == attempts:
                log.error("%s %s -> %s failed after %d attempts", what, src, dst, attempts)
                raise
            time.sleep(delay)
            delay = min(delay * 2, _MAX_DELAY)


def replace_with_retry(src: Path, dst: Path, attempts: int = DEFAULT_ATTEMPTS) -> None:
    """``os.replace`` with backoff. Overwrites ``dst`` if it exists."""
    _retry(os.replace, Path(src), Path(dst), attempts, "replace")


def rename_with_retry(src: Path, dst: Path, attempts: int = DEFAULT_ATTEMPTS) -> None:
    """``os.rename`` with backoff. ``dst`` must not exist; used for directory swaps."""
    _retry(os.rename, Path(src), Path(dst), attempts, "rename")


def excel_lock_files(directory: Path) -> list[Path]:
    """Excel's ``~$name.xlsx`` owner-lock files in ``directory``.

    Their presence means a workbook is open somewhere. Reading a workbook while
    Excel is mid-save returns a genuinely different file — during development a
    workbook caught in that window reported eight TEJ blocks and no header row —
    so every job checks for these before touching the raw layer.
    """
    return sorted(Path(directory).glob("~$*"))
