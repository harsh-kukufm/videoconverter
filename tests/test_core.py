"""Unit tests for critical pure logic.

No GUI toolkit, GPU, FFmpeg binary, or network access required.
"""
from __future__ import annotations

import datetime
import zipfile
from pathlib import Path

import pytest

from videoconverter.core.downloader import DownloadError, safe_extract_zip
from videoconverter.core.encoding import (
    ConversionSettings,
    compute_scaling,
    decode_return_code,
    sanitize_command,
)
from videoconverter.core.ffmpeg_manager import (
    UpdateStatus,
    _parse_btbn_date,
    _parse_ffmpeg_list,
    _parse_hwaccels,
    _parse_semver,
)
from videoconverter.core.hardware import Backend, BackendInfo
from videoconverter.core.platform_info import ArchType, OSType, PlatformInfo


# ----------------------------------------------------------- platform info

def test_platform_detect_runs():
    p = PlatformInfo.detect()
    assert p.os in {OSType.WINDOWS, OSType.MACOS, OSType.LINUX, OSType.UNKNOWN}
    assert p.arch in {ArchType.X86_64, ArchType.ARM64, ArchType.X86, ArchType.UNKNOWN}
    assert p.exe_suffix in (".exe", "")


# ------------------------------------------------------------ scaling logic

@pytest.mark.parametrize("w,h,maxd,expected", [
    (3840, 2160, 1920, (1920, 1080)),
    (1920, 1080, 1920, None),
    (1080, 1920, 1920, None),
    (2160, 3840, 1920, (1080, 1920)),
    (2560, 1080, 1920, (1920, 810)),
    (0, 100, 1920, None),
    (100, 100, 1920, None),
])
def test_compute_scaling(w, h, maxd, expected):
    assert compute_scaling(w, h, maxd) == expected


def test_scaling_even_dimensions():
    nw, nh = compute_scaling(3841, 2161, 1920)
    assert nw % 2 == 0 and nh % 2 == 0


# ------------------------------------------------- exit code / sanitisation

def test_decode_return_code_windows_unsigned():
    assert decode_return_code(4294967274) == -22
    assert decode_return_code(0) == 0
    assert decode_return_code(1) == 1


def test_sanitize_command_strips_directories():
    cmd = ["/usr/bin/ffmpeg", "-i", "/home/user/videos/clip.mp4", "out.mp4"]
    s = sanitize_command(cmd)
    assert "/usr/bin/ffmpeg" not in s
    assert "/home/user/videos/clip.mp4" not in s
    assert "ffmpeg" in s and "clip.mp4" in s and "out.mp4" in s


# ------------------------------------------------- ffmpeg output parsing

def test_parse_ffmpeg_list():
    sample = """
 Encoders:
 V..... = Video
 ------
 V....D libx265              libx265 H.265 / HEVC (codec hevc)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 A....D aac                  AAC (Advanced Audio Coding)
"""
    names = _parse_ffmpeg_list(sample)
    assert "libx265" in names
    assert "hevc_nvenc" in names
    assert "aac" in names


def test_parse_hwaccels():
    sample = "Hardware acceleration methods:\nvaapi\ncuda\nvideotoolbox\n"
    assert _parse_hwaccels(sample) == {"vaapi", "cuda", "videotoolbox"}


def test_parse_btbn_date():
    line = "ffmpeg version N-121105-ga0936b9769-20250917 Copyright (c) ..."
    assert _parse_btbn_date(line) == datetime.date(2025, 9, 17)


def test_parse_btbn_date_rejects_non_btbn():
    assert _parse_btbn_date("ffmpeg version 7.0.2 Copyright (c)") is None


def test_parse_semver():
    assert _parse_semver("ffmpeg version 7.0.2") == (7, 0, 2)
    assert _parse_semver("ffmpeg version 6.1-static") == (6, 1, 0)
    assert _parse_semver("not ffmpeg") is None


# ------------------------------------------------------ archive safety

def test_zip_traversal_blocked(tmp_path: Path):
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "boom")
        zf.writestr("safe.txt", "ok")
    dest = tmp_path / "extract"
    with pytest.raises(DownloadError):
        safe_extract_zip(archive, dest)


def test_zip_normal_extraction(tmp_path: Path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("sub/file.txt", "hi")
    dest = tmp_path / "extract"
    safe_extract_zip(archive, dest)
    assert (dest / "sub" / "file.txt").read_text() == "hi"


# ------------------------------------------------------ backend model

def test_backend_labels_nonempty():
    for b in Backend:
        assert b.value.strip()


def test_backend_info_defaults():
    b = BackendInfo(Backend.CPU, "libx265")
    assert not b.valid
    assert b.hwaccel is None


def test_update_status_values():
    assert UpdateStatus.NOT_INSTALLED.value == "not_installed"
    assert UpdateStatus.UPDATE_AVAILABLE.value == "update_available"