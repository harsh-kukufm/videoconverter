"""Discovery of FFmpeg installed outside the app's managed directory.

Package managers install FFmpeg into well-known locations that a GUI
application may not see via its own PATH (Finder-launched macOS apps
get a minimal PATH; a Windows app started before `winget install ffmpeg`
misses the new PATH entry). This module checks the process PATH first,
then those well-known locations, then the persisted Windows PATH.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .platform_info import PlatformInfo

log = logging.getLogger("videoconverter.ffmpeg")

WhichFn = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class SystemFFmpeg:
    """An FFmpeg candidate found outside the managed directory.

    `ffprobe` may be None when only the ffmpeg binary was found; the
    caller decides whether to skip such candidates.
    """

    path: Path
    ffprobe: Optional[Path]
    origin: str


def _path_origin(p: Path) -> str:
    """Best-effort guess of which package manager owns this path."""
    s = str(p).replace("\\", "/").lower()
    if "homebrew" in s or "linuxbrew" in s:
        return "Homebrew"
    if "/opt/local/" in s:
        return "MacPorts"
    if "winget" in s:
        return "winget"
    if "chocolatey" in s:
        return "Chocolatey"
    if "scoop" in s:
        return "Scoop"
    if "/snap/" in s:
        return "snap"
    return "system"


UPDATE_HINTS: dict[str, str] = {
    "Homebrew": "brew upgrade ffmpeg",
    "MacPorts": "sudo port upgrade ffmpeg",
    "winget": "winget upgrade Gyan.FFmpeg",
    "Chocolatey": "choco upgrade ffmpeg",
    "Scoop": "scoop update ffmpeg",
    "snap": "sudo snap refresh ffmpeg",
    "system package manager": "your system package manager",
}


def update_hint(origin: str) -> str:
    """A user-facing command (or phrase) for updating this install."""
    return UPDATE_HINTS.get(origin, "your system package manager")


def _win_env(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default)


def well_known_dirs(platform: PlatformInfo) -> list[tuple[Path, str]]:
    """Directories where package managers are known to place FFmpeg.

    Ordered by how "intentional" the install is: package-manager
    directories before generic manual-install locations.
    """
    if platform.is_windows:
        local = _win_env(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local")
        progfiles = _win_env("ProgramFiles", Path(r"C:\Program Files"))
        progdata = _win_env("ProgramData", Path(r"C:\ProgramData"))
        return [
            (local / "Microsoft" / "WinGet" / "Links", "winget"),
            (progfiles / "WinGet" / "Links", "winget"),
            (progdata / "chocolatey" / "bin", "Chocolatey"),
            (Path.home() / "scoop" / "shims", "Scoop"),
            (progfiles / "ffmpeg" / "bin", "manual install"),
            (Path(r"C:\ffmpeg\bin"), "manual install"),
        ]
    if platform.is_macos:
        return [
            (Path("/opt/homebrew/bin"), "Homebrew"),
            (Path("/usr/local/bin"), "Homebrew"),
            (Path("/opt/local/bin"), "MacPorts"),
        ]
    if platform.is_linux:
        return [
            (Path("/usr/bin"), "system package manager"),
            (Path("/usr/local/bin"), "manual install"),
            (Path("/snap/bin"), "snap"),
            (Path("/home/linuxbrew/.linuxbrew/bin"), "Homebrew"),
            (Path.home() / ".linuxbrew" / "bin", "Homebrew"),
        ]
    return []


def _registry_path_dirs() -> list[Path]:
    """Directories on the *persisted* Windows PATH (HKCU + HKLM).

    A GUI app started before `winget install ffmpeg` still runs with the
    old PATH environment; the registry holds the current one.
    """
    import winreg  # local import - Windows only

    dirs: list[Path] = []
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]
    for root, subkey in keys:
        try:
            with winreg.OpenKey(root, subkey) as key:
                raw, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if not isinstance(raw, str):
            continue
        for part in raw.split(";"):
            part = part.strip().strip('"')
            if not part:
                continue
            dirs.append(Path(os.path.expandvars(part)))
    return dirs


def find_system_ffmpeg(
    platform: PlatformInfo,
    which_fn: WhichFn = shutil.which,
    extra_dirs: Optional[list[tuple[Path, str]]] = None,
    well_known: Optional[list[tuple[Path, str]]] = None,
    registry_dirs_fn: Optional[Callable[[], list[Path]]] = None,
) -> list[SystemFFmpeg]:
    """Find candidate system FFmpeg installs, best guess first.

    Order: the process PATH (respects the user's own overrides), then
    well-known package-manager locations, then — Windows only — the
    persisted PATH registry entries. Duplicate binaries are dropped.

    `well_known` and `registry_dirs_fn` override the machine scan for
    testing; production callers leave them unset.
    """
    suffix = platform.exe_suffix
    ff_name = "ffmpeg" + suffix
    fp_name = "ffprobe" + suffix

    candidates: list[SystemFFmpeg] = []
    seen: set[Path] = set()

    def consider(ff: Path, fp: Optional[Path], origin: str) -> None:
        try:
            key = ff.resolve()
        except OSError:
            key = ff
        if key in seen:
            return
        seen.add(key)
        candidates.append(SystemFFmpeg(ff, fp, origin))

    # 1. The process PATH.
    hit = which_fn(ff_name)
    if hit:
        ff = Path(hit)
        fp_hit = which_fn(fp_name)
        if not fp_hit:
            sibling = ff.with_name(fp_name)
            if sibling.is_file():
                fp_hit = str(sibling)
        consider(ff, Path(fp_hit) if fp_hit else None, _path_origin(ff))

    # 2. Well-known package-manager locations.
    dirs = well_known if well_known is not None else well_known_dirs(platform)
    dirs = [*dirs, *(extra_dirs or [])]
    seen_dirs: set[Path] = set()
    for d, origin in dirs:
        try:
            key = d.resolve()
        except OSError:
            continue
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        ff = d / ff_name
        if not ff.is_file():
            continue
        fp = d / fp_name
        consider(ff, fp if fp.is_file() else None, origin)

    # 3. Windows: the persisted PATH may be newer than this process's.
    if platform.is_windows:
        registry_fn = registry_dirs_fn or _registry_path_dirs
        for d in registry_fn():
            ff = d / ff_name
            if not ff.is_file():
                continue
            fp = d / fp_name
            consider(ff, fp if fp.is_file() else None, _path_origin(ff))

    return candidates
