"""GPU detection and validated backend selection."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .ffmpeg_manager import FFmpegManager
from .platform_info import PlatformInfo
from .util import no_window_kwargs

log = logging.getLogger("videoconverter.hardware")


class Backend(str, Enum):
    NVIDIA_NVENC = "NVIDIA NVENC"
    AMD_AMF = "AMD AMF"
    AMD_VAAPI = "AMD VAAPI"
    INTEL_VAAPI = "Intel VAAPI"
    APPLE_VIDEOTOOLBOX = "Apple VideoToolbox"
    CPU = "CPU"


@dataclass
class GPUInfo:
    nvidia: bool = False
    amd: bool = False
    intel: bool = False
    apple: bool = False
    nvidia_name: str = ""


@dataclass
class BackendInfo:
    backend: Backend
    encoder: str
    hwaccel: Optional[str] = None
    valid: bool = False
    reason: str = ""


class HardwareDetector:
    def __init__(self, platform: PlatformInfo, ffmpeg: FFmpegManager):
        self.platform = platform
        self.ffmpeg = ffmpeg
        self.gpu = self._detect_gpu()
        self._selected: Optional[BackendInfo] = None

    # ---------------------------------------------------------------- GPU layer

    def _detect_gpu(self) -> GPUInfo:
        g = GPUInfo()
        if self.platform.is_macos:
            # Both Intel Macs and Apple Silicon have VideoToolbox
            g.apple = True
        if shutil.which("nvidia-smi"):
            try:
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=5,
                    **no_window_kwargs(),
                )
                if r.returncode == 0 and r.stdout.strip():
                    g.nvidia = True
                    g.nvidia_name = r.stdout.strip().splitlines()[0]
            except Exception:
                pass
        if self.platform.is_linux and os.path.exists("/dev/dri/renderD128"):
            # We cannot always tell AMD vs Intel without lspci; VAAPI will be
            # chosen and validated by an actual test encode.
            g.amd = True  # best-effort hint
        return g

    # ----------------------------------------------------------- backend layer

    def select_backend(self) -> BackendInfo:
        if self._selected is not None:
            return self._selected
        try:
            caps = self.ffmpeg.inspect_capabilities()
        except Exception as e:
            log.exception("Capability inspection failed: %s", e)
            self._selected = BackendInfo(Backend.CPU, "libx265", valid=True)
            return self._selected

        candidates: list[BackendInfo] = []

        if self.platform.is_macos and caps.has_encoder("hevc_videotoolbox"):
            candidates.append(BackendInfo(Backend.APPLE_VIDEOTOOLBOX,
                                          "hevc_videotoolbox", "videotoolbox"))

        if caps.has_encoder("hevc_nvenc"):
            candidates.append(BackendInfo(Backend.NVIDIA_NVENC, "hevc_nvenc", "cuda"))

        if caps.has_encoder("hevc_amf"):
            candidates.append(BackendInfo(Backend.AMD_AMF, "hevc_amf"))

        if caps.has_encoder("hevc_vaapi") and "vaapi" in caps.hwaccels:
            if self.gpu.amd:
                candidates.append(BackendInfo(Backend.AMD_VAAPI, "hevc_vaapi", "vaapi"))
            else:
                candidates.append(BackendInfo(Backend.INTEL_VAAPI, "hevc_vaapi", "vaapi"))

        for c in candidates:
            log.info("Testing backend %s (%s)…", c.backend.value, c.encoder)
            if self._test_backend(c):
                c.valid = True
                self._selected = c
                log.info("Selected backend: %s (%s)", c.backend.value, c.encoder)
                return c
            log.warning("Backend %s not usable: %s", c.backend.value, c.reason)

        # CPU fallback
        for enc in ("libx265", "libx264", "hevc"):
            if caps.has_encoder(enc):
                self._selected = BackendInfo(Backend.CPU, enc, valid=True)
                log.info("Falling back to CPU encoder: %s", enc)
                return self._selected
        self._selected = BackendInfo(Backend.CPU, "libx265", valid=True,
                                     reason="No encoders reported; attempting libx265")
        return self._selected

    def _test_backend(self, info: BackendInfo) -> bool:
        if not self.ffmpeg.ffmpeg_path:
            info.reason = "ffmpeg not available"
            return False

        cmd = [
            str(self.ffmpeg.ffmpeg_path),
            "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=s=256x256:d=0.2:r=10",
        ]
        if info.backend in (Backend.AMD_VAAPI, Backend.INTEL_VAAPI):
            if not os.path.exists("/dev/dri/renderD128"):
                info.reason = "/dev/dri/renderD128 not available"
                return False
            cmd += ["-vaapi_device", "/dev/dri/renderD128",
                    "-vf", "format=nv12,hwupload"]
        cmd += ["-c:v", info.encoder, "-frames:v", "1", "-f", "null", "-"]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                               **no_window_kwargs())
            if r.returncode == 0:
                return True
            tail = (r.stderr or "").strip().splitlines()
            info.reason = tail[-1] if tail else f"exit {r.returncode}"
            return False
        except subprocess.TimeoutExpired:
            info.reason = "test encode timed out"
            return False
        except Exception as e:
            info.reason = str(e)
            return False