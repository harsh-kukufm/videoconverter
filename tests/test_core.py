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
    FFmpegError,
    FFmpegManager,
    UpdateStatus,
    _evermeet_zip_url,
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


def _platform(os_t: OSType, arch: ArchType) -> PlatformInfo:
    return PlatformInfo(os_t, arch, "test", 64, "3.12", False, False)


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


# ------------------------------------------------- ffmpeg artifact sources

def test_evermeet_zip_url_extracts_download():
    info = {
        "version": "9.0.2",
        "download": {"zip": {"url": "https://evermeet.cx/ffmpeg/ffmpeg-9.0.2.zip"}},
    }
    assert _evermeet_zip_url(info) == "https://evermeet.cx/ffmpeg/ffmpeg-9.0.2.zip"


def test_evermeet_zip_url_rejects_bad_payload():
    with pytest.raises(FFmpegError):
        _evermeet_zip_url({})
    with pytest.raises(FFmpegError):
        _evermeet_zip_url(None)  # type: ignore[arg-type]


def test_artifact_urls_windows_and_linux_static():
    win = FFmpegManager(_platform(OSType.WINDOWS, ArchType.X86_64))
    urls = [u for u, _ in win.artifact_urls()]
    assert len(urls) == 1
    assert urls[0].startswith("https://github.com/BtbN/FFmpeg-Builds/")
    assert "win64" in urls[0]

    lin = FFmpegManager(_platform(OSType.LINUX, ArchType.X86_64))
    lin_urls = [u for u, _ in lin.artifact_urls()]
    assert lin_urls[0].startswith("https://johnvansickle.com/ffmpeg/")
    assert "amd64" in lin_urls[0]


def test_macos_artifact_urls_resolved_via_info_api(monkeypatch):
    """macOS URLs must be resolved live from the evermeet info API.

    The old /getrelease/zip shortcuts return HTTP 410 Gone, so the manager
    must query /info/<tool>/release per tool instead of using static URLs.
    """
    requested: list[str] = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "download": {
                    "zip": {"url": "https://evermeet.cx/ffmpeg/ffmpeg-9.0.2.zip"}
                }
            }

    def fake_get(url, **kwargs):
        requested.append(url)
        return FakeResponse()

    monkeypatch.setattr(
        "videoconverter.core.ffmpeg_manager.requests.get", fake_get
    )

    mgr = FFmpegManager(_platform(OSType.MACOS, ArchType.ARM64))
    urls = mgr.artifact_urls()

    assert all(u.startswith("https://evermeet.cx/ffmpeg/") and u.endswith(".zip")
               for u, _ in urls)
    assert all(ext == ".zip" for _, ext in urls)
    # One info-API request per required tool: ffmpeg and ffprobe.
    assert any("/info/ffmpeg/release" in u for u in requested)
    assert any("/info/ffprobe/release" in u for u in requested)


def test_check_for_update_not_installed_needs_no_network():
    mgr = FFmpegManager(_platform(OSType.LINUX, ArchType.X86_64))
    check = mgr.check_for_update()
    assert check.status is UpdateStatus.NOT_INSTALLED


def test_ffplay_path_defaults_to_none():
    mgr = FFmpegManager(_platform(OSType.MACOS, ArchType.ARM64))
    assert mgr.ffplay_path is None


def _make_zip(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, b"stub binary")


def _patch_install_env(monkeypatch, ffplay_download_fails: bool):
    """Stub the network, archive and validation layers for install()."""
    import videoconverter.core.ffmpeg_manager as fm_mod

    def fake_artifact_url(self, tool, timeout=15.0):
        return f"https://evermeet.cx/ffmpeg/{tool}-9.0.2.zip", ".zip"

    def fake_download_file(url, dest, progress_cb=None, cancel_cb=None, **kw):
        if ffplay_download_fails and "ffplay" in url:
            raise fm_mod.DownloadError("connection reset")
        if "ffprobe" in url:
            _make_zip(dest, ["ffprobe"])
        elif "ffplay" in url:
            _make_zip(dest, ["ffplay"])
        else:
            _make_zip(dest, ["ffmpeg"])

    monkeypatch.setattr(
        fm_mod.FFmpegManager, "_evermeet_artifact_url", fake_artifact_url
    )
    monkeypatch.setattr(fm_mod, "download_file", fake_download_file)
    monkeypatch.setattr(
        fm_mod.FFmpegManager, "_binary_runs", lambda self, p: True
    )


def test_macos_install_survives_failed_ffplay_download(monkeypatch, tmp_path):
    _patch_install_env(monkeypatch, ffplay_download_fails=True)
    mgr = FFmpegManager(_platform(OSType.MACOS, ArchType.ARM64),
                        ffmpeg_root=tmp_path / "root")
    mgr.install()
    assert (mgr.ffmpeg_root / "ffmpeg").exists()
    assert (mgr.ffmpeg_root / "ffprobe").exists()
    assert mgr.ffplay_path is None


def test_macos_install_installs_all_three_tools(monkeypatch, tmp_path):
    _patch_install_env(monkeypatch, ffplay_download_fails=False)
    mgr = FFmpegManager(_platform(OSType.MACOS, ArchType.ARM64),
                        ffmpeg_root=tmp_path / "root")
    mgr.install()
    assert (mgr.ffmpeg_root / "ffmpeg").exists()
    assert (mgr.ffmpeg_root / "ffprobe").exists()
    assert mgr.ffplay_path == mgr.ffmpeg_root / "ffplay"


def test_update_status_values():
    assert UpdateStatus.NOT_INSTALLED.value == "not_installed"
    assert UpdateStatus.UPDATE_AVAILABLE.value == "update_available"