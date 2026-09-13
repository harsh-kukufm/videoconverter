"""Rotating file logging + non-blocking GUI log delivery."""
from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
from pathlib import Path


class QueueLogHandler(logging.Handler):
    """Push formatted records onto a queue. Never touches Qt widgets."""

    def __init__(self, q: "queue.Queue[str]", level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: Path, level: int = logging.INFO) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "videoconverter.log"

    logger = logging.getLogger("videoconverter")
    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=4 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)
    logger.addHandler(sh)

    return logger, log_file