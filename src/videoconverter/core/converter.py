"""FFmpeg command construction and the conversion worker thread."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .ffmpeg_manager import FFmpegManager
from .hardware import Backend, BackendInfo
from .jobs import ConversionJob, JobState
from .media_probe import MediaInfo, MediaProbe
from .util import no_window_kwargs

log = logging.getLogger("videoconverter.converter")

MP4_AUDIO_COPY_OK = {"aac", "mp3", "ac3", "eac3", "alac"}


# ----------------------------------------------------------------- settings

@dataclass
class ConversionSettings:
    max_resolution: int = 1920
    quality: str = "balanced"
    audio_bitrate: str = "192k"
    overwrite: bool = False
    output_option: int = 2       # 1 custom, 2 same-as-source
    custom_output_dir: str = ""


# ----------------------------------------------------------------- scaling

def _even(n: int) -> int:
    return n - (n % 2)


def compute_scaling(width: int, height: int, max_dim: int) -> Optional[tuple[int, int]]:
    """Return (w, h) if downscale is required. Never upscales. Never returns a
    dimension below 2."""
    if width <= 0 or height <= 0 or max_dim <= 0:
        return None
    longest = max(width, height)
    if longest <= max_dim:
        return None
    scale = max_dim / longest
    nw = max(2, _even(int(width * scale)))
    nh = max(2, _even(int(height * scale)))
    return nw, nh


# ---------------------------------------------------------- command builder

class FFmpegCommandBuilder:
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

        vf: list[str] = []
        if backend.backend in (Backend.AMD_VAAPI, Backend.INTEL_VAAPI):
            cmd += ["-vaapi_device", "/dev/dri/renderD128"]

        scaling = compute_scaling(media.width, media.height, settings.max_resolution)
        if scaling:
            vf.append(f"scale={scaling[0]}:{scaling[1]}:flags=lanczos")

        if backend.backend in (Backend.AMD_VAAPI, Backend.INTEL_VAAPI):
            vf.append("format=nv12")
            vf.append("hwupload")

        if vf:
            cmd += ["-vf", ",".join(vf)]

        # ------------------------------------------ encoder-specific arguments
        enc = backend.encoder
        q = settings.quality

        if backend.backend is Backend.CPU:
            cmd += ["-c:v", enc]
            if enc in ("libx265", "libx264"):
                preset = {"fast": "fast", "balanced": "medium", "quality": "slow"}.get(q, "medium")
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
            quality = {"fast": "speed", "balanced": "balanced", "quality": "quality"}.get(q, "balanced")
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
            # Defensive last-resort
            cmd += ["-c:v", "libx265", "-preset", "medium", "-crf", "24", "-tag:v", "hvc1"]

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


# ------------------------------------------------------------------- worker

class ConversionWorker(QThread):
    progress = Signal(int, int, str)          # index, percent, status
    log_line = Signal(str)
    job_state_changed = Signal(int, str)
    finished_all = Signal(int, int)           # success_count, total

    def __init__(
        self,
        jobs: list[ConversionJob],
        ffmpeg: FFmpegManager,
        backend: BackendInfo,
        settings: ConversionSettings,
        parent=None,
    ):
        super().__init__(parent)
        self.jobs = jobs
        self.ffmpeg = ffmpeg
        self.backend = backend
        self.settings = settings
        self._probe = MediaProbe(ffmpeg)
        self._builder = FFmpegCommandBuilder(ffmpeg)
        self._cancel = threading.Event()
        self._proc_lock = threading.Lock()
        self._current_proc: Optional[subprocess.Popen] = None

    # --------------------------------------------------------------- control

    def cancel(self) -> None:
        self._cancel.set()
        with self._proc_lock:
            proc = self._current_proc
        if proc and proc.poll() is None:
            self._terminate_tree(proc)

    @staticmethod
    def _terminate_tree(proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, **no_window_kwargs(),
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as e:
            log.warning("Failed to terminate process: %s", e)

    # ------------------------------------------------------------- execution

    def run(self) -> None:
        success = 0
        total = len(self.jobs)
        for i, job in enumerate(self.jobs):
            if self._cancel.is_set():
                job.state = JobState.CANCELLED
                self.job_state_changed.emit(i, job.state.value)
                continue
            try:
                self._process(i, job)
                if job.state is JobState.COMPLETED:
                    success += 1
            except Exception as e:
                log.exception("Job failed: %s", e)
                job.state = JobState.FAILED
                job.error = str(e)
                self.log_line.emit(f"[FAIL] {job.input_path.name}: {e}")
            self.job_state_changed.emit(i, job.state.value)
        self.finished_all.emit(success, total)

    def _process(self, index: int, job: ConversionJob) -> None:
        in_path = job.input_path

        if not in_path.is_file():
            raise RuntimeError("Input is not a regular file")

        job.state = JobState.PROBING
        self.job_state_changed.emit(index, job.state.value)
        self.log_line.emit(f"[…] Probing {in_path.name}")

        media = self._probe.probe(in_path)
        job.media_info = media
        if media.duration <= 0:
            raise RuntimeError("Could not determine media duration")

        job.output_path = self._resolve_output(in_path)
        job.output_path.parent.mkdir(parents=True, exist_ok=True)

        job.backend = self.backend.backend.value
        job.state = JobState.CONVERTING
        self.job_state_changed.emit(index, job.state.value)

        final_path = self._unique_final(job.output_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)

        # FFmpeg picks its muxer from the output filename extension, so the
        # temporary file MUST keep the real container extension (e.g. ".mp4").
        # We only hide/uniquify it via a dotted prefix + random suffix.
        fd, tmp_str = tempfile.mkstemp(
            prefix=f".{final_path.stem}.vc_tmp_",
            suffix=final_path.suffix,
            dir=str(final_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_str)
        # FFmpeg will overwrite with -y; ensure we can delete on any failure.
        try:
            tmp_path.unlink()
        except OSError:
            pass

        cmd = self._builder.build(in_path, tmp_path, media, self.backend, self.settings)
        self.log_line.emit(
            f"[▶] {in_path.name} → {final_path.name} via {self.backend.backend.value}"
        )
        log.info("Command: %s", _sanitize_cmd(cmd))

        job.started_at = time.time()
        rc = self._run_ffmpeg(cmd, index, media.duration)
        job.finished_at = time.time()

        if self._cancel.is_set():
            tmp_path.unlink(missing_ok=True)
            job.state = JobState.CANCELLED
            return

        if rc != 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg exited with code {rc}")

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError("Output file missing or empty after conversion")

        # Optional post-probe
        try:
            self._probe.probe(tmp_path)
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Output validation failed: {e}")

        os.replace(tmp_path, final_path)
        job.output_path = final_path
        job.state = JobState.COMPLETED
        self.progress.emit(index, 100, "Completed")

    def _resolve_output(self, in_path: Path) -> Path:
        stem = in_path.stem + "_converted.mp4"
        if self.settings.output_option == 1 and self.settings.custom_output_dir:
            return Path(self.settings.custom_output_dir) / stem
        return in_path.parent / "converted_videos" / stem

    def _unique_final(self, desired: Path) -> Path:
        if self.settings.overwrite or not desired.exists():
            return desired
        for n in range(1, 10000):
            cand = desired.with_name(f"{desired.stem}_{n}{desired.suffix}")
            if not cand.exists():
                return cand
        raise RuntimeError("Could not find a free output filename")

    def _run_ffmpeg(self, cmd: list[str], index: int, duration: float) -> int:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            **no_window_kwargs(),
        )
        with self._proc_lock:
            self._current_proc = proc

        stderr_tail: list[str] = []

        def reader() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    stderr_tail.append(line.rstrip())
                    if len(stderr_tail) > 300:
                        del stderr_tail[:100]
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        last_emit = 0.0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._cancel.is_set():
                    self._terminate_tree(proc)
                    break
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k in ("out_time_ms", "out_time_us"):
                    try:
                        current = int(v) / 1_000_000.0
                    except ValueError:
                        continue
                    pct = int(min(100.0, (current / duration) * 100)) if duration > 0 else 0
                    now = time.time()
                    if now - last_emit >= 0.2:
                        last_emit = now
                        self.progress.emit(index, pct, f"{pct}%")
        finally:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._terminate_tree(proc)
            t.join(timeout=2)
            with self._proc_lock:
                self._current_proc = None

        if stderr_tail:
            for line in stderr_tail[-20:]:
                if line.strip():
                    log.debug("ffmpeg: %s", line)
        return proc.returncode


def _sanitize_cmd(cmd: list[str]) -> str:
    """Keep the command informative without leaking the full user path list."""
    out: list[str] = []
    for token in cmd:
        if os.sep in token or (os.altsep and os.altsep in token):
            out.append(Path(token).name)
        else:
            out.append(token)
    return " ".join(out)