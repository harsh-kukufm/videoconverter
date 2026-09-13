"""Pure-logic encoding helpers. No Qt, no subprocess, no I/O.

This module is deliberately import-light so it can be unit-tested without
a GUI toolkit, FFmpeg, or a display server.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .ffmpeg_manager import FFmpegManager
from .hardware import Backend, BackendInfo
from .media_probe import MediaInfo

# Audio codecs that are safe to stream-copy into an MP4 container.
MP4_AUDIO_COPY_OK = {"aac", "mp3", "ac3", "eac3", "alac"}


@dataclass
class ConversionSettings:
    max_resolution: int = 1920
    quality: str = "balanced"       # fast | balanced | quality
    audio_bitrate: str = "192k"
    overwrite: bool = False
    output_option: int = 2          # 1 custom, 2 same-as-source
    custom_output_dir: str = ""


def _even(n: int) -> int:
    return n - (n % 2)


def compute_scaling(width: int, height: int, max_dim: int) -> Optional[tuple[int, int]]:
    """Return (w, h) if downscale is required, else None.

    Rules:
      * Never upscales.
      * Downscales so the *longest* side equals max_dim.
      * Aspect ratio preserved.
      * Both dimensions forced even (HEVC / yuv420p requirement).
      * Never returns a dimension below 2.
    """
    if width <= 0 or height <= 0 or max_dim <= 0:
        return None
    longest = max(width, height)
    if longest <= max_dim:
        return None
    scale = max_dim / longest
    nw = max(2, _even(int(width * scale)))
    nh = max(2, _even(int(height * scale)))
    return nw, nh


def decode_return_code(rc: int) -> int:
    """Windows reports exit codes as unsigned 32-bit; convert back to signed."""
    if rc is not None and rc > 0x7FFFFFFF:
        return rc - 0x100000000
    return rc


def sanitize_command(cmd: list[str]) -> str:
    """Return a readable command string with directory components stripped.

    Keeps filenames (which are informative) but drops full user paths so
    logs don't leak local directory structure unnecessarily.
    """
    out: list[str] = []
    for token in cmd:
        if os.sep in token or (os.altsep and os.altsep in token):
            out.append(Path(token).name)
        else:
            out.append(token)
    return " ".join(out)


class FFmpegCommandBuilder:
    """Builds encoder-specific FFmpeg argument lists.

    Each hardware backend gets parameters that are actually valid for
    that encoder (e.g. `-cq` for NVENC, `-q:v` for VideoToolbox) rather
    than reusing libx265's `-crf`.
    """

    def __init__(self, ffmpeg: FFmpegManager):
        self.ffmpeg = ffmpeg

    def build(
        self,
        input_path: Path,
        output_path: Path,
        media: MediaInfo,
        backend: BackendInfo,
        settings: ConversionSettings,
    ) -> list[str]:
        if not self.ffmpeg.ffmpeg_path:
            raise RuntimeError("FFmpeg path not set")

        cmd: list[str] = [
            str(self.ffmpeg.ffmpeg_path),
            "-hide_banner", "-nostdin",
            "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            "-y",
            "-i", str(input_path),
        ]

        if backend.backend in (Backend.AMD_VAAPI, Backend.INTEL_VAAPI):
            cmd += ["-vaapi_device", "/dev/dri/renderD128"]

        vf: list[str] = []
        scaling = compute_scaling(media.width, media.height, settings.max_resolution)
        if scaling:
            vf.append(f"scale={scaling[0]}:{scaling[1]}:flags=lanczos")
        if backend.backend in (Backend.AMD_VAAPI, Backend.INTEL_VAAPI):
            vf.append("format=nv12")
            vf.append("hwupload")
        if vf:
            cmd += ["-vf", ",".join(vf)]

        # ------------------------------------------ encoder-specific args
        enc = backend.encoder
        q = settings.quality

        if backend.backend is Backend.CPU:
            cmd += ["-c:v", enc]
            if enc in ("libx265", "libx264"):
                preset = {"fast": "fast", "balanced": "medium",
                          "quality": "slow"}.get(q, "medium")
                crf = {"fast": "26", "balanced": "24", "quality": "21"}.get(q, "24")
                cmd += ["-preset", preset, "-crf", crf]
            cmd += ["-pix_fmt", "yuv420p"]
            if enc == "libx265":
                cmd += ["-tag:v", "hvc1"]

        elif backend.backend is Backend.NVIDIA_NVENC:
            cmd += ["-c:v", enc]
            preset = {"fast": "p3", "balanced": "p5", "quality": "p7"}.get(q, "p5")
            cq = {"fast": "28", "balanced": "24", "quality": "21"}.get(q, "24")
            cmd += ["-preset", preset, "-rc", "vbr", "-cq", cq, "-b:v", "0"]
            cmd += ["-tag:v", "hvc1"]

        elif backend.backend is Backend.AMD_AMF:
            cmd += ["-c:v", enc]
            quality = {"fast": "speed", "balanced": "balanced",
                       "quality": "quality"}.get(q, "balanced")
            cqp = {"fast": "26", "balanced": "24", "quality": "22"}.get(q, "24")
            cmd += ["-quality", quality, "-rc", "cqp",
                    "-qp_i", cqp, "-qp_p", cqp, "-qp_b", cqp]
            cmd += ["-tag:v", "hvc1"]

        elif backend.backend in (Backend.AMD_VAAPI, Backend.INTEL_VAAPI):
            cmd += ["-c:v", enc]
            qp = {"fast": "26", "balanced": "24", "quality": "22"}.get(q, "24")
            cmd += ["-qp", qp]
            cmd += ["-tag:v", "hvc1"]

        elif backend.backend is Backend.APPLE_VIDEOTOOLBOX:
            cmd += ["-c:v", enc]
            qv = {"fast": "50", "balanced": "65", "quality": "80"}.get(q, "65")
            cmd += ["-q:v", qv, "-tag:v", "hvc1"]

        else:
            # Defensive last resort
            cmd += ["-c:v", "libx265", "-preset", "medium",
                    "-crf", "24", "-tag:v", "hvc1"]

        # ------------------------------------------------ audio
        if media.nb_audio_streams == 0:
            cmd += ["-an"]
        else:
            ac = (media.audio_codec or "").lower()
            if ac in MP4_AUDIO_COPY_OK:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", settings.audio_bitrate]

        cmd += ["-movflags", "+faststart", str(output_path)]
        return cmd