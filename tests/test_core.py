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
    FFmpegManager,
    FFmpegSource,
    UpdateStatus,
    _parse_btbn_date,
    _parse_ffmpeg_list,
    _parse_hwaccels,
    _parse_semver,
)
from videoconverter.core.hardware import Backend, BackendInfo
from videoconverter.core.platform_info import ArchType, OSType, PlatformInfo
from videoconverter.core.system_ffmpeg import (
    SystemFFmpeg,
    _path_origin,
    find_system_ffmpeg,
    update_hint,
    well_known_dirs,
)


def _make_platform(os_t: OSType, arch: ArchType = ArchType.X86_64) -> PlatformInfo:
    return PlatformInfo(
        os=os_t, arch=arch, machine=arch.value, python_bitness=64,
        python_version="3.11", is_rosetta=False, is_apple_silicon=False,
    )


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


# ------------------------------------------------- system FFmpeg discovery

def test_well_known_dirs_cover_package_managers():
    mac = well_known_dirs(_make_platform(OSType.MACOS))
    assert (Path("/opt/homebrew/bin"), "Homebrew") in mac
    assert (Path("/opt/local/bin"), "MacPorts") in mac

    win = well_known_dirs(_make_platform(OSType.WINDOWS))
    assert any("WinGet" in str(d) and o == "winget" for d, o in win)
    assert any("chocolatey" in str(d).lower() and o == "Chocolatey"
               for d, o in win)
    assert any("scoop" in str(d).lower() and o == "Scoop" for d, o in win)

    linux = well_known_dirs(_make_platform(OSType.LINUX))
    assert (Path("/usr/bin"), "system package manager") in linux
    assert (Path("/snap/bin"), "snap") in linux


def test_path_origin_inference():
    assert _path_origin(Path("/opt/homebrew/bin/ffmpeg")) == "Homebrew"
    assert _path_origin(
        Path(r"C:\Users\u\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe")
    ) == "winget"
    assert _path_origin(
        Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe")
    ) == "Chocolatey"
    assert _path_origin(Path(r"C:\Users\u\scoop\shims\ffmpeg.exe")) == "Scoop"
    assert _path_origin(Path("/usr/bin/ffmpeg")) == "system"


def test_find_system_ffmpeg_prefers_path_and_dedups(tmp_path: Path):
    # A path containing "homebrew" so origin inference recognises it.
    brew = tmp_path / "opt" / "homebrew" / "bin"
    brew.mkdir(parents=True)
    (brew / "ffmpeg").write_bytes(b"x")
    (brew / "ffprobe").write_bytes(b"x")

    # ffprobe deliberately NOT on PATH: the sibling-directory fallback
    # must find it next to the ffmpeg PATH hit.
    def fake_which(name: str):
        return str(brew / name) if name == "ffmpeg" else None

    cands = find_system_ffmpeg(
        _make_platform(OSType.MACOS),
        which_fn=fake_which,
        well_known=[(brew, "Homebrew")],   # same dir again — must dedup
        registry_dirs_fn=lambda: [],
    )
    assert len(cands) == 1
    assert cands[0].path == brew / "ffmpeg"
    assert cands[0].ffprobe == brew / "ffprobe"
    assert cands[0].origin == "Homebrew"


def test_find_system_ffmpeg_flags_missing_ffprobe(tmp_path: Path):
    d = tmp_path / "only"
    d.mkdir()
    (d / "ffmpeg.exe").write_bytes(b"x")
    cands = find_system_ffmpeg(
        _make_platform(OSType.WINDOWS),
        which_fn=lambda name: None,
        well_known=[(d, "manual install")],
        registry_dirs_fn=lambda: [],
    )
    assert len(cands) == 1
    assert cands[0].path == d / "ffmpeg.exe"
    assert cands[0].ffprobe is None


def test_find_system_ffmpeg_none_found(tmp_path: Path):
    cands = find_system_ffmpeg(
        _make_platform(OSType.LINUX),
        which_fn=lambda name: None,
        well_known=[(tmp_path / "empty", "manual install")],
    )
    assert cands == []


def test_update_hint_mapping():
    assert update_hint("Homebrew") == "brew upgrade ffmpeg"
    assert update_hint("winget").startswith("winget")
    assert update_hint("something-else") == "your system package manager"


# ------------------------------------------------------- locate() ordering

def test_locate_prefers_managed_over_system(tmp_path: Path, monkeypatch):
    root = tmp_path / "ffmpeg"
    root.mkdir()
    (root / "ffmpeg").write_bytes(b"x")
    (root / "ffprobe").write_bytes(b"x")

    system_ff = tmp_path / "opt" / "ffmpeg"
    monkeypatch.setattr(
        "videoconverter.core.ffmpeg_manager.find_system_ffmpeg",
        lambda platform: [SystemFFmpeg(
            system_ff, tmp_path / "opt" / "ffprobe", "Homebrew")],
    )
    monkeypatch.setattr(FFmpegManager, "_binary_runs", lambda self, p: True)

    mgr = FFmpegManager(_make_platform(OSType.LINUX), ffmpeg_root=root)
    assert mgr.locate() is True
    assert mgr.source is FFmpegSource.MANAGED
    assert mgr.source_origin == "app-managed"
    assert mgr.ffmpeg_path == root / "ffmpeg"


def test_locate_falls_back_to_system_install(tmp_path: Path, monkeypatch):
    missing_root = tmp_path / "nonexistent"
    ff = tmp_path / "opt" / "ffmpeg"
    fp = tmp_path / "opt" / "ffprobe"
    ff.parent.mkdir()
    ff.write_bytes(b"x")
    fp.write_bytes(b"x")

    monkeypatch.setattr(
        "videoconverter.core.ffmpeg_manager.find_system_ffmpeg",
        lambda platform: [SystemFFmpeg(ff, fp, "Homebrew")],
    )
    monkeypatch.setattr(FFmpegManager, "_binary_runs", lambda self, p: True)

    mgr = FFmpegManager(_make_platform(OSType.MACOS), ffmpeg_root=missing_root)
    assert mgr.locate() is True
    assert mgr.source is FFmpegSource.SYSTEM
    assert mgr.source_origin == "Homebrew"
    assert mgr.ffmpeg_path == ff
    assert mgr.ffprobe_path == fp


def test_locate_skips_system_candidate_without_ffprobe(
    tmp_path: Path, monkeypatch,
):
    ff = tmp_path / "opt" / "ffmpeg"
    ff.parent.mkdir()
    ff.write_bytes(b"x")

    monkeypatch.setattr(
        "videoconverter.core.ffmpeg_manager.find_system_ffmpeg",
        lambda platform: [SystemFFmpeg(ff, None, "manual install")],
    )

    mgr = FFmpegManager(_make_platform(OSType.LINUX),
                        ffmpeg_root=tmp_path / "nonexistent")
    assert mgr.locate() is False
    assert mgr.source is FFmpegSource.NONE
    assert mgr.ffmpeg_path is None


# --------------------------------------------------- update-check routing

def test_check_for_update_skips_remote_for_system_install(
    tmp_path: Path, monkeypatch,
):
    ff = tmp_path / "ffmpeg"
    ff.write_bytes(b"x")
    mgr = FFmpegManager(_make_platform(OSType.MACOS), ffmpeg_root=tmp_path)
    mgr.ffmpeg_path = ff
    mgr.source = FFmpegSource.SYSTEM
    mgr.source_origin = "Homebrew"
    monkeypatch.setattr(
        mgr, "local_version_string", lambda: "ffmpeg version 7.0.2",
    )

    check = mgr.check_for_update()
    assert check.status is UpdateStatus.UNKNOWN
    assert "brew upgrade ffmpeg" in check.detail


def test_check_for_update_managed_linux_has_no_remote_metadata(
    tmp_path: Path, monkeypatch,
):
    ff = tmp_path / "ffmpeg"
    ff.write_bytes(b"x")
    mgr = FFmpegManager(_make_platform(OSType.LINUX), ffmpeg_root=tmp_path)
    mgr.ffmpeg_path = ff
    mgr.source = FFmpegSource.MANAGED
    monkeypatch.setattr(
        mgr, "local_version_string", lambda: "ffmpeg version 7.0.2",
    )

    check = mgr.check_for_update()
    assert check.status is UpdateStatus.UNKNOWN
    assert check.detail == "no remote metadata available"


def test_check_for_update_not_installed(tmp_path: Path):
    mgr = FFmpegManager(_make_platform(OSType.LINUX), ffmpeg_root=tmp_path)
    check = mgr.check_for_update()
    assert check.status is UpdateStatus.NOT_INSTALLED