"""Locate, install, and inspect FFmpeg."""
from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import requests

from . import config as cfg
from .downloader import (
    DownloadError,
    download_file,
    safe_extract_tar,
    safe_extract_zip,
)
from .platform_info import ArchType, OSType, PlatformInfo
from .util import no_window_kwargs

log = logging.getLogger("videoconverter.ffmpeg")


class FFmpegError(Exception):
    pass


@dataclass
class FFmpegCapabilities:
    version_line: str = ""
    encoders: set[str] = field(default_factory=set)
    decoders: set[str] = field(default_factory=set)
    hwaccels: set[str] = field(default_factory=set)
    filters: set[str] = field(default_factory=set)

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_hwaccel(self, name: str) -> bool:
        return name in self.hwaccels


_FFMPEG_LIST_RE = re.compile(r"^\s*[A-Z.]{6}\s+(\S+)")


def _parse_ffmpeg_list(output: str) -> set[str]:
    names: set[str] = set()
    for line in output.splitlines():
        m = _FFMPEG_LIST_RE.match(line)
        if m:
            names.add(m.group(1))
    return names


def _parse_hwaccels(output: str) -> set[str]:
    names: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Hardware acceleration"):
            continue
        if re.fullmatch(r"[A-Za-z0-9_]+", line):
            names.add(line)
    return names


class UpdateStatus(str, Enum):
    NOT_INSTALLED = "not_installed"
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    UNKNOWN = "unknown"


@dataclass
class UpdateCheck:
    status: UpdateStatus
    local_version: str = ""
    remote_version: str = ""
    detail: str = ""


# Regexes for local version parsing ----------------------------------------

# BtbN builds:  ffmpeg version N-121105-ga0936b9769-20250917
_BTBN_DATE_RE = re.compile(
    r"ffmpeg version\s+N-\d+-g[0-9a-fA-F]+-(\d{8})"
)
# evermeet / static builds:  ffmpeg version 7.0.2   (or 6.1, 6.1.1, ...)
_SEMVER_RE = re.compile(
    r"ffmpeg version\s+(?:n?)?(\d+)\.(\d+)(?:\.(\d+))?"
)


def _parse_btbn_date(version_line: str) -> Optional[datetime.date]:
    m = _BTBN_DATE_RE.search(version_line)
    if not m:
        return None
    raw = m.group(1)
    try:
        return datetime.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def _parse_semver(version_line: str) -> Optional[tuple[int, int, int]]:
    m = _SEMVER_RE.search(version_line)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


class FFmpegManager:
    def __init__(self, platform: PlatformInfo, ffmpeg_root: Optional[Path] = None):
        self.platform = platform
        self.ffmpeg_root = ffmpeg_root or cfg.ffmpeg_dir()
        self.ffmpeg_path: Optional[Path] = None
        self.ffprobe_path: Optional[Path] = None
        self.capabilities: Optional[FFmpegCapabilities] = None

    # ------------------------------------------------------------------ locate

    def _exe(self, name: str) -> str:
        return name + self.platform.exe_suffix

    def _find_binaries(self, root: Path) -> Optional[tuple[Path, Optional[Path]]]:
        ff_name = self._exe("ffmpeg")
        fp_name = self._exe("ffprobe")
        ff = fp = None
        for p in root.rglob("*"):
            if p.is_file() and not ff and p.name == ff_name:
                ff = p
            if p.is_file() and not fp and p.name == fp_name:
                fp = p
            if ff and fp:
                break
        if ff:
            return ff, fp
        return None

    def _binary_runs(self, ffmpeg: Path) -> bool:
        try:
            r = subprocess.run(
                [str(ffmpeg), "-version"],
                capture_output=True, text=True, timeout=10,
                **no_window_kwargs(),
            )
            return r.returncode == 0 and "ffmpeg version" in r.stdout
        except Exception:
            return False

    def locate(self) -> bool:
        if self.ffmpeg_root.exists():
            found = self._find_binaries(self.ffmpeg_root)
            if found and found[1] is not None \
                    and self._binary_runs(found[0]) \
                    and Path(found[1]).exists():
                self.ffmpeg_path = found[0]
                self.ffprobe_path = found[1]
                log.info("Using managed FFmpeg at %s", self.ffmpeg_path)
                log.info("Using managed ffprobe at %s", self.ffprobe_path)
                return True
            if found:
                log.warning(
                    "Managed FFmpeg at %s is incomplete "
                    "(ffprobe missing); falling back to PATH",
                    self.ffmpeg_root,
                )

        ff = shutil.which(self._exe("ffmpeg"))
        fp = shutil.which(self._exe("ffprobe"))
        if ff and fp:
            ffp, fpp = Path(ff), Path(fp)
            if self._binary_runs(ffp):
                self.ffmpeg_path = ffp
                self.ffprobe_path = fpp
                log.info("Using system FFmpeg at %s", self.ffmpeg_path)
                log.info("Using system ffprobe at %s", self.ffprobe_path)
                return True

        return False

    # ------------------------------------------------------------- capabilities

    def inspect_capabilities(self, force: bool = False) -> FFmpegCapabilities:
        if self.capabilities is not None and not force:
            return self.capabilities
        if not self.ffmpeg_path:
            raise FFmpegError("FFmpeg not located")

        caps = FFmpegCapabilities()
        caps.version_line = (
            self._run_text(["-version"]).splitlines()[0]
            if self.ffmpeg_path else ""
        )
        caps.encoders = _parse_ffmpeg_list(
            self._run_text(["-hide_banner", "-encoders"])
        )
        caps.decoders = _parse_ffmpeg_list(
            self._run_text(["-hide_banner", "-decoders"])
        )
        caps.hwaccels = _parse_hwaccels(
            self._run_text(["-hide_banner", "-hwaccels"])
        )
        # filters output has a different shape; use a tolerant parse
        caps.filters = _parse_ffmpeg_list(
            self._run_text(["-hide_banner", "-filters"])
        )
        self.capabilities = caps
        log.info(
            "FFmpeg capabilities: %d encoders, hwaccels=%s",
            len(caps.encoders), sorted(caps.hwaccels),
        )
        return caps

    def _run_text(self, args: list[str]) -> str:
        assert self.ffmpeg_path
        try:
            r = subprocess.run(
                [str(self.ffmpeg_path), *args],
                capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace",
                **no_window_kwargs(),
            )
            return (r.stdout or "") + "\n" + (r.stderr or "")
        except Exception as e:
            log.warning("ffmpeg %s failed: %s", args, e)
            return ""

    # ----------------------------------------------------------------- install

    # --------------------------------------------------------------- version

    def local_version_string(self) -> str:
        """Return the first line of `ffmpeg -version` (e.g. 'ffmpeg version N-...')."""
        if not self.ffmpeg_path:
            raise FFmpegError("FFmpeg not located")
        try:
            r = subprocess.run(
                [str(self.ffmpeg_path), "-version"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
                **no_window_kwargs(),
            )
        except Exception as e:
            raise FFmpegError(f"ffmpeg -version failed: {e}") from e
        if r.returncode != 0:
            raise FFmpegError(f"ffmpeg -version exited {r.returncode}")
        line = (r.stdout or "").strip().splitlines()
        return line[0].strip() if line else ""

    # ------------------------------------------------------------- update check

    def check_for_update(self, timeout: float = 8.0) -> UpdateCheck:
        """Compare the local build against the current one at its source.

        Never raises for network errors — returns UpdateStatus.UNKNOWN instead.
        Only returns UPDATE_AVAILABLE when a *confirmed* newer build exists.
        """
        if not self.ffmpeg_path or not Path(self.ffmpeg_path).exists():
            return UpdateCheck(UpdateStatus.NOT_INSTALLED)

        try:
            local_version = self.local_version_string()
        except FFmpegError as e:
            log.info("Could not read local FFmpeg version: %s", e)
            return UpdateCheck(UpdateStatus.UNKNOWN, detail="local version unreadable")

        try:
            url, _ = self.artifact_url()
        except FFmpegError as e:
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_version,
                               detail=str(e))

        try:
            if "BtbN" in url:
                return self._check_btbn(local_version, timeout)
            if "evermeet" in url:
                return self._check_evermeet(local_version, timeout)
            # No reliable remote metadata endpoint for this source
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_version,
                               detail="no remote metadata available")
        except Exception as e:
            log.info("Update check failed: %s", e)
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_version,
                               detail=f"network error: {type(e).__name__}")

    # --------------------------------------------------- per-source checks

    def _check_btbn(self, local_version_line: str, timeout: float) -> UpdateCheck:
        local_date = _parse_btbn_date(local_version_line)
        if local_date is None:
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_version_line,
                               detail="local build is not a BtbN build")

        r = requests.get(
            "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest",
            timeout=timeout,
            headers={
                "User-Agent": "VideoConverter/0.2",
                "Accept": "application/vnd.github+json",
            },
        )
        r.raise_for_status()
        data = r.json()
        published = data.get("published_at") or ""
        if not published:
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_date.isoformat(),
                               detail="remote release has no published_at")

        try:
            remote_date = datetime.date.fromisoformat(published.split("T", 1)[0])
        except ValueError:
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_date.isoformat(),
                               detail="remote date unparseable")

        if remote_date > local_date:
            return UpdateCheck(
                UpdateStatus.UPDATE_AVAILABLE,
                local_version=local_date.isoformat(),
                remote_version=remote_date.isoformat(),
                detail=f"BtbN {remote_date} > local {local_date}",
            )
        return UpdateCheck(
            UpdateStatus.UP_TO_DATE,
            local_version=local_date.isoformat(),
            remote_version=remote_date.isoformat(),
        )

    def _check_evermeet(self, local_version_line: str, timeout: float) -> UpdateCheck:
        local_ver = _parse_semver(local_version_line)
        if local_ver is None:
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_version_line,
                               detail="could not parse local version")

        r = requests.get(
            "https://evermeet.cx/ffmpeg/info/ffmpeg/release",
            timeout=timeout,
            headers={"User-Agent": "VideoConverter/0.2",
                     "Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        remote_str = str(data.get("version") or "")
        remote_ver = _parse_semver(f"ffmpeg version {remote_str}")
        if remote_ver is None:
            return UpdateCheck(UpdateStatus.UNKNOWN, local_version=local_version_line,
                               detail="could not parse remote version")

        local_s = ".".join(map(str, local_ver))
        remote_s = ".".join(map(str, remote_ver))
        if remote_ver > local_ver:
            return UpdateCheck(
                UpdateStatus.UPDATE_AVAILABLE,
                local_version=local_s,
                remote_version=remote_s,
            )
        return UpdateCheck(UpdateStatus.UP_TO_DATE,
                           local_version=local_s, remote_version=remote_s)

    def artifact_urls(self) -> list[tuple[str, str]]:
        """Return a list of (url, extension) pairs for the current platform.

        Most platforms ship a single archive containing both ffmpeg and ffprobe.
        macOS is the exception: evermeet.cx publishes ffmpeg and ffprobe as
        separate universal (Intel + Apple Silicon) archives.
        """
        o, a = self.platform.os, self.platform.arch

        if o is OSType.WINDOWS:
            if a is ArchType.ARM64:
                return [(
                    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                    "ffmpeg-master-latest-winarm64-gpl.zip", ".zip",
                )]
            return [(
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                "ffmpeg-master-latest-win64-gpl.zip", ".zip",
            )]

        if o is OSType.MACOS:
            # evermeet.cx builds are universal binaries and work on both
            # Intel and Apple Silicon. ffmpeg and ffprobe come as separate zips.
            return [
                ("https://evermeet.cx/ffmpeg/getrelease/zip",  ".zip"),
                ("https://evermeet.cx/ffprobe/getrelease/zip", ".zip"),
            ]

        if o is OSType.LINUX:
            if a is ArchType.ARM64:
                return [(
                    "https://johnvansickle.com/ffmpeg/releases/"
                    "ffmpeg-release-arm64-static.tar.xz", ".tar.xz",
                )]
            return [(
                "https://johnvansickle.com/ffmpeg/releases/"
                "ffmpeg-release-amd64-static.tar.xz", ".tar.xz",
            )]

        raise FFmpegError(f"Unsupported platform: {o} / {a}")

    def install(
        self,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> None:
        artifacts = self.artifact_urls()

        def emit(pct: int, msg: str) -> None:
            log.info("FFmpeg install: %s%% %s", pct, msg)
            if progress_cb:
                progress_cb(pct, msg)

        emit(0, f"Downloading FFmpeg ({self.platform.describe()})…")

        with tempfile.TemporaryDirectory(prefix="vc_ffmpeg_") as tmpdir:
            tmp = Path(tmpdir)
            extract_root = tmp / "extract"
            extract_root.mkdir()

            n = len(artifacts)
            for i, (url, ext) in enumerate(artifacts):
                base = int((i / n) * 85)
                span = int(85 / n)
                archive = tmp / f"artifact_{i}{ext}"

                def dl_progress(done: int, total: int,
                                _base=base, _span=span, _i=i) -> None:
                    pct = _base + (int((done / total) * _span) if total else 0)
                    emit(min(pct, 85),
                         f"Downloading {_i + 1}/{n}: "
                         f"{done // (1024 * 1024)} MiB")

                download_file(url, archive,
                              progress_cb=dl_progress, cancel_cb=cancel_cb)
                if cancel_cb and cancel_cb():
                    raise DownloadError("Cancelled")

                if ext == ".zip":
                    safe_extract_zip(archive, extract_root)
                else:
                    safe_extract_tar(archive, extract_root)

            emit(88, "Locating binaries…")
            found = self._find_binaries(extract_root)
            if not found:
                raise FFmpegError("No ffmpeg binary found in downloaded archives")

            src_ffmpeg, src_ffprobe = found

            if src_ffprobe is None or not src_ffprobe.exists():
                raise FFmpegError(
                    "Downloaded archives did not contain ffprobe. "
                    "This is a packaging error in the upstream build; "
                    "please report it."
                )

            if not self._binary_runs(src_ffmpeg):
                raise FFmpegError("Downloaded ffmpeg failed validation")

            emit(96, "Installing atomically…")
            self._atomic_install(src_ffmpeg, src_ffprobe)

            self.capabilities = None
            emit(100, "FFmpeg installed")
            try:
                self.inspect_capabilities(force=True)
            except FFmpegError:
                pass

    def _atomic_install(self, src_ffmpeg: Path, src_ffprobe: Path) -> None:
        if not src_ffprobe or not src_ffprobe.exists():
            raise FFmpegError(
                "Refusing to install: ffprobe missing from downloaded payload"
            )

        root = self.ffmpeg_root
        parent = root.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / (root.name + ".staging")
        backup = parent / (root.name + ".backup")

        for p in (staging, backup):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        staging.mkdir(parents=True)

        try:
            tgt_ff = staging / src_ffmpeg.name
            tgt_fp = staging / src_ffprobe.name
            shutil.copy2(src_ffmpeg, tgt_ff)
            shutil.copy2(src_ffprobe, tgt_fp)

            for p in (tgt_ff, tgt_fp):
                try:
                    os.chmod(p, p.stat().st_mode
                             | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                except OSError:
                    pass

            if not self._binary_runs(tgt_ff):
                raise FFmpegError("Staged ffmpeg failed validation")

            if root.exists():
                os.rename(root, backup)
            os.rename(staging, root)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)

            self.ffmpeg_path = root / src_ffmpeg.name
            self.ffprobe_path = root / src_ffprobe.name
        except Exception:
            if backup.exists() and not root.exists():
                try:
                    os.rename(backup, root)
                except OSError:
                    pass
            shutil.rmtree(staging, ignore_errors=True)
            raise