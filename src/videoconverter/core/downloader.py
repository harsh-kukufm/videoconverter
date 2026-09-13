"""Safe streaming download and safe archive extraction (no path traversal)."""
from __future__ import annotations

import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests


class DownloadError(Exception):
    pass


def _safe_target(base: Path, member_name: str) -> Path:
    name = member_name.replace("\\", "/").lstrip("/")
    # Reject absolute Windows paths and drive letters
    if ":" in name.split("/", 1)[0]:
        raise DownloadError(f"Unsafe archive entry: {member_name}")
    target = (base / name).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        raise DownloadError(f"Path traversal in archive: {member_name}")
    return target


def safe_extract_zip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            target = _safe_target(dest, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = info.external_attr >> 16
            if mode:
                try:
                    os.chmod(target, mode & 0o777)
                except OSError:
                    pass


def safe_extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            target = _safe_target(dest, member.name)
            if member.issym() or member.islnk():
                # Only allow links that resolve inside dest
                link_base = target.parent if member.issym() else dest
                try:
                    (link_base / member.linkname).resolve().relative_to(dest_resolved)
                except ValueError:
                    continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                os.chmod(target, member.mode & 0o777)
            except OSError:
                pass


def download_file(
    url: str,
    dest: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    timeout: float = 60.0,
    max_size: int = 500 * 1024 * 1024,
) -> None:
    """Stream `url` into `dest`. Writes `dest.part` and atomically renames."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    part.unlink(missing_ok=True)

    headers = {"User-Agent": "VideoConverter/0.2 (+python-requests)"}
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=headers,
                          allow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0) or 0)
            if total and total > max_size:
                raise DownloadError(f"Refusing download > {max_size} bytes ({total})")
            done = 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancel_cb and cancel_cb():
                        raise DownloadError("Download cancelled")
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if done > max_size:
                        raise DownloadError("Download exceeded size limit")
                    if progress_cb:
                        progress_cb(done, total)
        part.replace(dest)
    except Exception:
        part.unlink(missing_ok=True)
        raise