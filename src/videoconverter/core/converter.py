"""Qt-threaded conversion worker. All pure logic lives in `encoding.py`."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .encoding import (
    ConversionSettings,
    FFmpegCommandBuilder,
    decode_return_code,
    sanitize_command,
)
from .ffmpeg_manager import FFmpegManager
from .hardware import BackendInfo
from .jobs import ConversionJob, JobState
from .media_probe import MediaProbe
from .util import no_window_kwargs

log = logging.getLogger("videoconverter.converter")


class ConversionWorker(QThread):
    progress = Signal(int, int, str)           # index, percent, status
    log_line = Signal(str)
    job_state_changed = Signal(int, str)
    finished_all = Signal(int, int)            # success_count, total

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
        # temp file MUST keep the real container extension (e.g. ".mp4").
        fd, tmp_str = tempfile.mkstemp(
            prefix=f".{final_path.stem}.vc_tmp_",
            suffix=final_path.suffix,
            dir=str(final_path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            tmp_path.unlink()
        except OSError:
            pass

        cmd = self._builder.build(in_path, tmp_path, media, self.backend, self.settings)
        self.log_line.emit(
            f"[▶] {in_path.name} → {final_path.name} via {self.backend.backend.value}"
        )
        log.info("Command: %s", sanitize_command(cmd))

        job.started_at = time.time()
        rc = self._run_ffmpeg(cmd, index, media.duration)
        job.finished_at = time.time()

        if self._cancel.is_set():
            tmp_path.unlink(missing_ok=True)
            job.state = JobState.CANCELLED
            return

        if rc != 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg exited with code {decode_return_code(rc)}")

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError("Output file missing or empty after conversion")

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