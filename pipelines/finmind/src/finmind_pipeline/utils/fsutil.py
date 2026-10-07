"""Filesystem primitives that tolerate Windows' transient locks.

``os.replace`` and ``os.rename`` are atomic on Windows, but they fail outright
with ``PermissionError`` (WinError 5) whenever another process holds a handle on
the source or target — most often a real-time antivirus scan of a file that was
just written, or the shell indexer. The lock is short-lived, so a bounded
backoff clears it.

This matters more than it sounds. During a 6,300-call backfill the rate-limiter
state file is rewritten on every single call; at roughly one failure per 2,000
writes an unguarded ``os.replace`` silently drops a ticker every few minutes.
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
    """``os.rename`` with backoff. Used for directory swaps, where ``dst`` must not exist."""
    _retry(os.rename, Path(src), Path(dst), attempts, "rename")
