"""Qt GUI. All worker interaction goes through signals/slots."""
from __future__ import annotations

import logging
import os
import queue
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QTextEdit, QVBoxLayout, QWidget,
)

from .core import config as cfg
from .core.converter import ConversionSettings, ConversionWorker
from .core.ffmpeg_manager import (
    FFmpegError,
    FFmpegManager,
    UpdateCheck,
    UpdateStatus,
)
from .core.hardware import Backend, BackendInfo, HardwareDetector
from .core.jobs import ConversionJob, JobState
from .core.logging_setup import QueueLogHandler, setup_logging
from .core.platform_info import PlatformInfo

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv")


def play_notification() -> None:
    """Best-effort cross-platform notification sound. Never raises."""
    try:
        if sys.platform == "win32":
            import winsound  # local import - only on Windows
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


# ---------------------------------------------------------------- setup worker

class SetupWorker(QThread):
    progress = Signal(int, str)
    log_line = Signal(str)
    done = Signal(bool)

    def __init__(self, ffmpeg: FFmpegManager, parent=None):
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            self.ffmpeg.install(
                progress_cb=lambda p, m: self.progress.emit(p, m),
                cancel_cb=lambda: self._cancel,
            )
            self.done.emit(True)
        except Exception as e:
            self.log_line.emit(f"FFmpeg install failed: {e}")
            self.done.emit(False)

from dataclasses import dataclass


@dataclass
class StartupResult:
    ffmpeg_found: bool
    backend_label: str = "CPU"
    backend_encoder: str = "libx265"
    message: str = ""
    update_status: str = UpdateStatus.UNKNOWN.value
    update_local: str = ""
    update_remote: str = ""
    update_detail: str = ""

# ------------------------------------------------------------ startup worker

class StartupWorker(QThread):
    """Locate FFmpeg, inspect capabilities, pick a backend, check for updates."""

    log_line = Signal(str)
    ready = Signal(object)  # StartupResult

    def __init__(
        self,
        ffmpeg: FFmpegManager,
        platform: PlatformInfo,
        skip_update_check: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.platform = platform
        self.skip_update_check = skip_update_check

    def run(self) -> None:
        result = StartupResult(ffmpeg_found=False)
        try:
            self.log_line.emit(f"Platform: {self.platform.describe()}")

            if not self.ffmpeg.locate():
                result.message = "FFmpeg not found"
                result.update_status = UpdateStatus.NOT_INSTALLED.value
                return

            result.ffmpeg_found = True
            self.log_line.emit(f"FFmpeg: {self.ffmpeg.ffmpeg_path}")

            try:
                detector = HardwareDetector(self.platform, self.ffmpeg)
                backend = detector.select_backend()
                result.backend_label = backend.backend.value
                result.backend_encoder = backend.encoder
                self.log_line.emit(
                    f"Selected backend: {backend.backend.value} ({backend.encoder})"
                )
            except Exception as e:
                self.log_line.emit(f"Backend selection failed: {e}")

            if self.skip_update_check:
                # We just installed the current release; don't re-hit the network.
                try:
                    local_v = self.ffmpeg.local_version_string()
                except Exception:
                    local_v = ""
                check = UpdateCheck(UpdateStatus.UP_TO_DATE, local_version=local_v)
            else:
                check = self.ffmpeg.check_for_update()

            result.update_status = check.status.value
            result.update_local = check.local_version
            result.update_remote = check.remote_version
            result.update_detail = check.detail

            if check.status is UpdateStatus.UPDATE_AVAILABLE:
                self.log_line.emit(
                    f"FFmpeg update available: "
                    f"{check.local_version} → {check.remote_version}"
                )
            elif check.status is UpdateStatus.UP_TO_DATE:
                self.log_line.emit(f"FFmpeg is up to date ({check.local_version})")
            else:
                self.log_line.emit(
                    f"FFmpeg update status: {check.status.value}"
                    + (f" ({check.detail})" if check.detail else "")
                )
        except Exception as e:
            self.log_line.emit(f"Startup detection error: {e}")
            result.message = str(e)
        finally:
            self.ready.emit(result)


# ------------------------------------------------------------------ main GUI

class MainWindow(QMainWindow):
    def __init__(
        self,
        platform: PlatformInfo,
        ffmpeg: FFmpegManager,
        app_config: cfg.AppConfig,
        logger: logging.Logger,
        log_file: Path,
    ):
        super().__init__()
        self.platform = platform
        self.ffmpeg = ffmpeg
        self.config = app_config
        self.logger = logger
        self._log_file = log_file

        self.input_files: list[Path] = []
        self._jobs: list[ConversionJob] = []
        self._backend: Optional[BackendInfo] = None
        self._worker: Optional[ConversionWorker] = None
        self._setup_worker: Optional[SetupWorker] = None

        self.setWindowTitle("Video Converter")
        self.resize(self.config.window_w, self.config.window_h)
        self._apply_icon()
        self._apply_stylesheet()

        self._build_ui()

        # Queue-based log pumping
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        handler = QueueLogHandler(self._log_queue)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                               datefmt="%H:%M:%S"))
        logger.addHandler(handler)

        self._log_timer = QTimer(self)
        self._log_timer.setInterval(200)
        self._log_timer.timeout.connect(self._drain_log_queue)
        self._log_timer.start()

        self._append_log(f"Log file: {self._log_file}")
        self._append_log(f"FFmpeg dir: {self.ffmpeg.ffmpeg_root}")

        # Kick off startup detection
        self._start_detection()

    # ----------------------------------------------------------- UI building

    def _apply_icon(self) -> None:
        candidates = [
            Path(__file__).parent / "resources" / "icon.ico",
            Path(__file__).parent / "icon.ico",
            Path(sys.executable).parent / "icon.ico" if getattr(sys, "frozen", False) else Path(),
        ]
        for p in candidates:
            if p and p.exists():
                icon = QIcon(str(p))
                if not icon.isNull():
                    self.setWindowIcon(icon)
                    return

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #2D2D30; color: #DCDCDC; }
            QGroupBox { font-weight: bold; border: 1px solid #3E3E42;
                        border-radius: 5px; margin-top: 1ex; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px;
                               padding: 0 5px 0 5px; }
            QPushButton { background-color: #ff4081; color: white; border: none;
                          border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #e91e63; }
            QPushButton:pressed { background-color: #c2185b; }
            QPushButton:disabled { background-color: #555555; color: #999999; }
            QListWidget, QTextEdit, QLineEdit {
                background-color: #1E1E1E; border: 1px solid #3E3E42;
                border-radius: 4px; padding: 2px; }
            QProgressBar { border: 1px solid #3E3E42; border-radius: 4px;
                           text-align: center; background-color: #1E1E1E; }
            QProgressBar::chunk { background-color: #ff4081; }
            QRadioButton { color: #DCDCDC; spacing: 8px; }
        """)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)

        # Input
        inp = QGroupBox("Input Files")
        il = QVBoxLayout(inp)
        self.file_list = QListWidget()
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QListWidget.DropOnly)
        il.addWidget(self.file_list)
        row = QHBoxLayout()
        b_add = QPushButton("Add Files"); b_add.clicked.connect(self._add_files)
        b_clr = QPushButton("Clear List"); b_clr.clicked.connect(self._clear_files)
        row.addWidget(b_add); row.addWidget(b_clr)
        il.addLayout(row)
        main.addWidget(inp)

        # Environment
        env = QGroupBox("Environment")
        el = QVBoxLayout(env)
        self.gpu_label = QLabel("Hardware acceleration: detecting…")
        self.ffmpeg_label = QLabel("FFmpeg: not set up")
        el.addWidget(self.gpu_label)
        el.addWidget(self.ffmpeg_label)
        row2 = QHBoxLayout()
        self.setup_button = QPushButton("Setup FFmpeg")
        self.setup_button.setToolTip("Detecting FFmpeg status…")
        self.setup_button.clicked.connect(self._run_setup)
        self.cancel_setup_button = QPushButton("Cancel")
        self.cancel_setup_button.clicked.connect(self._cancel_setup)
        self.cancel_setup_button.setEnabled(False)
        row2.addWidget(self.setup_button); row2.addWidget(self.cancel_setup_button)
        el.addLayout(row2)
        main.addWidget(env)

        # Output
        out = QGroupBox("Output Location")
        ol = QVBoxLayout(out)
        self.output_group = QButtonGroup(self)
        self.radio_custom = QRadioButton("Custom directory")
        self.radio_same = QRadioButton("Same as source (creates 'converted_videos')")
        self.output_group.addButton(self.radio_custom, 1)
        self.output_group.addButton(self.radio_same, 2)
        (self.radio_custom if self.config.output_option == 1 else self.radio_same).setChecked(True)
        self.output_group.idToggled.connect(self._on_output_changed)

        ol.addWidget(self.radio_custom)
        row3 = QHBoxLayout()
        self.custom_dir_edit = QLineEdit(self.config.custom_output_dir)
        self.custom_dir_edit.setReadOnly(True)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse_output)
        row3.addWidget(self.custom_dir_edit); row3.addWidget(self.browse_button)
        ol.addLayout(row3)
        ol.addWidget(self.radio_same)
        self._on_output_changed()
        main.addWidget(out)

        # Conversion
        conv = QGroupBox("Conversion")
        cl = QVBoxLayout(conv)
        row4 = QHBoxLayout()
        self.convert_button = QPushButton("Convert Videos")
        self.convert_button.clicked.connect(self._start_conversion)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._stop_conversion)
        self.stop_button.setEnabled(False)
        row4.addWidget(self.convert_button); row4.addWidget(self.stop_button)
        cl.addLayout(row4)
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        cl.addWidget(self.progress)
        self.status = QLabel("Ready")
        cl.addWidget(self.status)
        main.addWidget(conv)

        # Log
        lg = QGroupBox("Log")
        ll = QVBoxLayout(lg)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(3000)
        ll.addWidget(self.log_view)
        main.addWidget(lg)

    # -------------------------------------------------------------- logging

    def _append_log(self, msg: str) -> None:
        self.log_view.append(msg)

    def _drain_log_queue(self) -> None:
        for _ in range(200):
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(msg)

    # ---------------------------------------------------------- startup flow

    def _start_detection(self, skip_update_check: bool = False) -> None:
        # Prevent overlapping detection runs
        prev = getattr(self, "_startup", None)
        if prev is not None and prev.isRunning():
            return
        self._startup = StartupWorker(
            self.ffmpeg, self.platform, skip_update_check=skip_update_check
        )
        self._startup.log_line.connect(self._append_log)
        self._startup.ready.connect(self._on_startup_ready)
        self._startup.start()

    def _on_startup_ready(self, result: StartupResult) -> None:
        if result.ffmpeg_found:
            self.ffmpeg_label.setText(f"FFmpeg: {self.ffmpeg.ffmpeg_path}")
            try:
                det = HardwareDetector(self.platform, self.ffmpeg)
                self._backend = det.select_backend()
                self.gpu_label.setText(
                    f"Hardware acceleration: {self._backend.backend.value} "
                    f"({self._backend.encoder})"
                )
            except Exception:
                self.logger.exception("Backend selection failed")
                self.gpu_label.setText(
                    "Hardware acceleration: CPU (detection failed)"
                )
                self._backend = None
        else:
            self.ffmpeg_label.setText(
                "FFmpeg: not found — click 'Setup FFmpeg'"
            )
            self.gpu_label.setText(
                f"Hardware acceleration: {result.backend_label}"
            )

        self._apply_update_status(result)

    def _apply_update_status(self, result: StartupResult) -> None:
        status = result.update_status
        if not result.ffmpeg_found or status == UpdateStatus.NOT_INSTALLED.value:
            self.setup_button.setText("Setup FFmpeg")
            self.setup_button.setToolTip("Download and install FFmpeg")
            return

        if status == UpdateStatus.UPDATE_AVAILABLE.value:
            self.setup_button.setText("Update FFmpeg")
            self.setup_button.setToolTip(
                f"Update available: "
                f"{result.update_local} → {result.update_remote}"
            )
            return

        # UP_TO_DATE or UNKNOWN — never claim an update exists.
        self.setup_button.setText("Reinstall FFmpeg")
        if status == UpdateStatus.UP_TO_DATE.value:
            self.setup_button.setToolTip(
                f"Already up to date ({result.update_local})"
            )
        else:
            self.setup_button.setToolTip(
                "Cannot determine remote version; "
                "reinstall to refresh the local build"
            )

    # -------------------------------------------------------- setup handler

    def _run_setup(self) -> None:
        if self._setup_worker and self._setup_worker.isRunning():
            return
        self.setup_button.setEnabled(False)
        self.cancel_setup_button.setEnabled(True)
        self._append_log("Starting FFmpeg setup…")

        self._setup_worker = SetupWorker(self.ffmpeg)
        self._setup_worker.progress.connect(
            lambda p, m: (self.progress.setValue(p), self.status.setText(m))
        )
        self._setup_worker.log_line.connect(self._append_log)
        self._setup_worker.done.connect(self._setup_done)
        self._setup_worker.start()

    def _cancel_setup(self) -> None:
        if self._setup_worker and self._setup_worker.isRunning():
            self._setup_worker.cancel()
            self._append_log("Cancelling FFmpeg setup…")

    def _setup_done(self, ok: bool) -> None:
        self.setup_button.setEnabled(True)
        self.cancel_setup_button.setEnabled(False)
        if ok:
            self.ffmpeg_label.setText(f"FFmpeg: {self.ffmpeg.ffmpeg_path}")
            play_notification()
            # Re-run detection; skip the network roundtrip since we *just*
            # installed the current release.
            self._start_detection(skip_update_check=True)
        else:
            self._append_log("FFmpeg setup did not complete")

    # -------------------------------------------------------- input handling

    def _add_files(self) -> None:
        start_dir = self.config.last_input_dir or str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select video files", start_dir,
            "Video files (*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.ts *.flv);;All files (*)",
        )
        if files:
            self.config.last_input_dir = str(Path(files[0]).parent)
            self.config.save()
        for f in files:
            self._add_path(Path(f))

    def _add_path(self, p: Path) -> None:
        if p in self.input_files or not p.is_file():
            return
        if p.suffix.lower() not in VIDEO_EXTENSIONS:
            return
        self.input_files.append(p)
        self.file_list.addItem(p.name)

    def _clear_files(self) -> None:
        self.input_files.clear()
        self.file_list.clear()
        self._append_log("Cleared file list")

    def dragEnterEvent(self, event):  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # type: ignore[override]
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self._add_path(Path(url.toLocalFile()))

    # -------------------------------------------------------- output options

    def _on_output_changed(self) -> None:
        is_custom = self.radio_custom.isChecked()
        self.browse_button.setEnabled(is_custom)
        self.custom_dir_edit.setEnabled(is_custom)

    def _browse_output(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select output directory",
            self.config.custom_output_dir or str(Path.home()),
            QFileDialog.ShowDirsOnly,
        )
        if d:
            self.config.custom_output_dir = d
            self.config.save()
            self.custom_dir_edit.setText(d)

    # ---------------------------------------------------------------- convert

    def _build_settings(self) -> ConversionSettings:
        out_opt = 1 if self.radio_custom.isChecked() else 2
        self.config.output_option = out_opt
        self.config.custom_output_dir = self.custom_dir_edit.text()
        self.config.save()
        return ConversionSettings(
            max_resolution=self.config.max_resolution,
            quality=self.config.quality,
            audio_bitrate=self.config.audio_bitrate,
            overwrite=self.config.overwrite,
            output_option=out_opt,
            custom_output_dir=self.config.custom_output_dir,
        )

    def _start_conversion(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        if not self.input_files:
            QMessageBox.warning(self, "No Files", "Add at least one video file.")
            return
        if not self.ffmpeg.ffmpeg_path:
            QMessageBox.warning(self, "FFmpeg missing",
                                "FFmpeg is not installed. Click 'Setup / Update FFmpeg'.")
            return
        if self.radio_custom.isChecked() and not self.config.custom_output_dir:
            QMessageBox.warning(self, "No Directory", "Choose a custom output directory.")
            return

        # Choose backend
        try:
            detector = HardwareDetector(self.platform, self.ffmpeg)
            backend = detector.select_backend()
        except Exception as e:
            self.logger.exception("Backend selection failed")
            QMessageBox.critical(self, "Backend error", str(e))
            return
        self._backend = backend

        settings = self._build_settings()

        self._jobs = [ConversionJob(input_path=Path(p)) for p in self.input_files]

        self.convert_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText("Starting…")

        self._worker = ConversionWorker(self._jobs, self.ffmpeg, backend, settings)
        self._worker.progress.connect(self._on_progress)
        self._worker.log_line.connect(self._append_log)
        self._worker.job_state_changed.connect(self._on_job_state)
        self._worker.finished_all.connect(self._on_all_done)
        self._worker.start()

    def _on_progress(self, idx: int, pct: int, status: str) -> None:
        # weighted overall progress
        if not self._jobs:
            return
        per = 100 // len(self._jobs)
        overall = min(100, idx * per + (pct * per // 100))
        self.progress.setValue(overall)
        self.status.setText(f"[{idx + 1}/{len(self._jobs)}] {status}")

    def _on_job_state(self, idx: int, state: str) -> None:
        if idx < len(self._jobs):
            self._jobs[idx].state = JobState(state)
            job = self._jobs[idx]
            tag = {"Completed": "✓", "Failed": "✗", "Cancelled": "⊘",
                   "Skipped": "…"}.get(state, "·")
            self._append_log(f"{tag} [{idx + 1}] {job.input_path.name}: {state}")

    def _on_all_done(self, ok: int, total: int) -> None:
        self.convert_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.setValue(100 if ok == total else self.progress.value())
        self.status.setText(f"Done: {ok}/{total} converted")
        self._append_log(f"Batch complete: {ok}/{total} succeeded")
        play_notification()

    def _stop_conversion(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.status.setText("Stopping…")

    # -------------------------------------------------------------- shutdown

    def closeEvent(self, event):  # type: ignore[override]
        try:
            if self._worker and self._worker.isRunning():
                self._worker.cancel()
                self._worker.wait(5000)
            if self._setup_worker and self._setup_worker.isRunning():
                self._setup_worker.cancel()
                self._setup_worker.wait(3000)
            if hasattr(self, "_startup") and self._startup.isRunning():
                self._startup.wait(2000)
        except Exception:
            pass
        self.config.window_w = self.width()
        self.config.window_h = self.height()
        try:
            self.config.save()
        except Exception:
            pass
        super().closeEvent(event)


# ------------------------------------------------------------------- entry

def main() -> int:
    platform = PlatformInfo.detect()

    log_dir = cfg.user_log_dir()
    logger, log_file = setup_logging(log_dir)

    app_config = cfg.AppConfig.load()
    level = getattr(logging, app_config.log_level.upper(), logging.INFO)
    logger.setLevel(level)

    ffmpeg = FFmpegManager(platform)

    app = QApplication(sys.argv)
    app.setApplicationName("Video Converter")

    window = MainWindow(platform, ffmpeg, app_config, logger, log_file)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())