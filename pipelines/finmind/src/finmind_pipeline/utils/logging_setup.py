"""Console + per-day file logging.

Log files are UTF-8 because the data contains Chinese security names and
industry labels; the Windows console default codepage would mangle them.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

from finmind_pipeline.settings import get_settings

_CONFIGURED: set[str] = set()

_FMT = "%(asctime)s %(levelname)-7s %(name)-38s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(kind: str = "ingestion", level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger to write to logs/<kind>/<today>.log and stderr.

    kind is normally "ingestion" or "processing". Safe to call repeatedly.
    """
    settings = get_settings()
    log_dir: Path = settings.logs_dir / kind
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{dt.date.today().isoformat()}.log"

    root = logging.getLogger()
    root.setLevel(level)

    key = f"{kind}:{log_file}"
    if key not in _CONFIGURED:
        formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        # Never let an un-encodable character kill a run on a cp950 console.
        if hasattr(stream.stream, "reconfigure"):
            try:
                stream.stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
        root.addHandler(stream)

        _CONFIGURED.add(key)
        root.info("logging to %s", log_file)

    # urllib3 logs every connection at DEBUG; far too chatty for a 6000-call run.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
