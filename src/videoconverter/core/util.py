"""Small cross-platform helpers."""
from __future__ import annotations

import subprocess
import sys
from typing import Any


def no_window_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs that suppress a console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}
    return {}


def run_hidden(cmd: list[str], timeout: float = 15.0, **kw) -> subprocess.CompletedProcess:
    """Run a subprocess with no visible console and safe text handling."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **no_window_kwargs(),
        **kw,
    )