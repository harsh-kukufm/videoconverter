"""Single-invocation, structured ffprobe wrapper."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .ffmpeg_manager import FFmpegManager
from .util import no_window_kwargs

log = logging.getLogger("videoconverter.probe")


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: Optional[str]
    audio_channels: Optional[int]
    audio_sample_rate: Optional[int]
    nb_audio_streams: int
    format_name: str
    size_bytes: int
    raw: dict[str, Any]


class MediaProbe:
    def __init__(self, ffmpeg: FFmpegManager):
        self.ffmpeg = ffmpeg

    def probe(self, path: Path) -> MediaInfo:
        if not self.ffmpeg.ffprobe_path or not Path(self.ffmpeg.ffprobe_path).exists():
            raise RuntimeError("ffprobe is not available")
        cmd = [
            str(self.ffmpeg.ffprobe_path),
            "-v", "error",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace",
                           **no_window_kwargs())
        if r.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {(r.stderr or '').strip()}")
        data = json.loads(r.stdout or "{}")
        return self._build(path, data)

    @staticmethod
    def _build(path: Path, data: dict[str, Any]) -> MediaInfo:
        streams = data.get("streams", []) or []
        fmt = data.get("format", {}) or {}

        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        audio = audios[0] if audios else None

        def _int(v, default=0) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        def _float(v, default=0.0) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        duration = _float(fmt.get("duration"), 0.0)
        if duration <= 0 and video:
            duration = _float(video.get("duration"), 0.0)

        return MediaInfo(
            path=path,
            duration=duration,
            width=_int(video.get("width")) if video else 0,
            height=_int(video.get("height")) if video else 0,
            video_codec=(video.get("codec_name") or "") if video else "",
            audio_codec=(audio.get("codec_name") if audio else None),
            audio_channels=_int(audio.get("channels")) if audio else None,
            audio_sample_rate=_int(audio.get("sample_rate")) if audio else None,
            nb_audio_streams=len(audios),
            format_name=fmt.get("format_name", "") or "",
            size_bytes=_int(fmt.get("size")),
            raw=data,
        )