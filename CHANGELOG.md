Changelog
=========

All notable changes to this project are documented in this file.
The format is based on `Keep a Changelog <https://keepachangelog.com/en/1.1.0/>`_
and this project adheres to `Semantic Versioning <https://semver.org/spec/v2.0.0.html>`_.


[0.2.0] - 2026-09-13
--------------------

Complete architectural rewrite. The application is now a
cross-platform, production-quality converter with a real hardware
acceleration pipeline.

### Added
- Cross-platform support: Windows x64/arm64, macOS Intel and Apple
  Silicon, Linux x64/arm64.
- Dedicated `PlatformInfo` layer detecting OS, CPU architecture, Python
  bitness, and Apple Silicon / Rosetta status.
- Layered GPU detection (`HardwareDetector`) verifying *actual* FFmpeg
  capability before selecting a backend, with a real one-frame null
  encode to validate each candidate.
- Hardware backends: NVIDIA NVENC, AMD AMF, AMD/Intel VAAPI, Apple
  VideoToolbox. CPU fallback (`libx265`) always available.
- `FFmpegManager` with platform-aware artifact selection, safe
  streaming download, checksum-free but validated atomic install, and
  rollback to the previous install on failure.
- Real update check per source (BtbN, evermeet). The GUI only shows
  "Update FFmpeg" when a newer build genuinely exists; otherwise
  "Setup FFmpeg" (not installed) or "Reinstall FFmpeg" (up to date /
  unknown).
- `MediaProbe` using a single structured `ffprobe -print_format json`
  invocation per file.
- `FFmpegCapabilities` cached once per process; no repeated
  `ffmpeg -encoders` calls.
- `FFmpegCommandBuilder` producing backend-specific encoder arguments
  (CRF/CQ/QP/preset/qp_i/qp_p/qp_b/quality) instead of passing libx265
  options to hardware encoders.
- Progress via `-progress pipe:1` and `out_time_ms`, throttled to
  ~5 Hz.
- Proper cancellation: `threading.Event`, whole process-tree kill via
  `taskkill /F /T` on Windows and terminate→kill escalation on Unix.
- Safe archive extraction (`safe_extract_zip` / `safe_extract_tar`)
  with path-traversal, drive-letter, and symlink escape protection.
- Atomic output strategy: hidden `.vc_tmp_*.<ext>` file with the
  correct container extension, ffprobe validation, atomic
  `os.replace` to the final name, uniquified filename when the target
  already exists and `overwrite=false`.
- Rotating file logs (`RotatingFileHandler`, 4 MiB × 5) in
  platform-appropriate directories.
- Non-blocking GUI logging via `QueueLogHandler` + `QTimer` drain.
- `AppConfig` persisted to a platform-specific config directory.
- Unit tests covering scaling math, FFmpeg output parsing, safe
  archive extraction, backend model, and version parsing.
- GitHub Actions workflow building Windows, macOS Intel, macOS Apple
  Silicon, and Linux artifacts.

### Changed
- Default output location is *alongside the source* in a
  `converted_videos/` subdirectory. The previous "Default (AppData)"
  option has been removed from the GUI.
- The GUI no longer touches Qt widgets from worker threads; all worker
  communication uses Qt signals.
- The "Setup / Update FFmpeg" button now reflects the actual state:
  `Setup FFmpeg` / `Update FFmpeg` / `Reinstall FFmpeg`, with an
  explanatory tooltip.
- Video scaling is now based on the *longest side* of the source,
  aspect-ratio preserving, even-dimension aligned, and never upscales.
- Audio is copied only when the source codec is container-compatible
  with MP4 (`aac`, `mp3`, `ac3`, `eac3`, `alac`); otherwise it is
  re-encoded to AAC at the configured bitrate.

### Fixed
- `winsound` was imported unconditionally, breaking startup on macOS
  and Linux. Now imported lazily and only on Windows.
- Hard-coded `ffmpeg.exe` broke every non-Windows platform. Executable
  resolution is now driven by `PlatformInfo.exe_suffix`.
- `ZipFile.extractall()` allowed zip-slip. Replaced with safe
  extraction.
- HTML scraping of GitHub release pages was brittle and unverified.
  Replaced with the GitHub release JSON API and direct artifact URLs.
- Passing `-crf` to `hevc_nvenc` / `hevc_amf` / `hevc_videotoolbox`
  produced invalid encoder options and failed conversions. Each
  backend now receives encoder-appropriate quality arguments.
- Unconditional `scale=iw:ih` forced an unnecessary resample; it is
  now only added when a real downscale is required.
- Scaling logic mis-handled portrait and ultrawide sources
  (`if width > 1920 or height > 1920`). Replaced with longest-side
  computation.
- Cancellation left orphan FFmpeg children on Windows. Now uses
  `taskkill /F /T`.
- Log flooding froze the GUI. Log delivery is now queued and throttled.
- Log path used a space-separated `appdirs` name mismatching the
  advertised directory. Unified under `core.config`.
- `detect_gpu()` called `self.log_message.emit(...)` on an object with
  no such signal, raising `AttributeError` on non-NVIDIA paths.
  Eliminated in the rewrite.
- `setup_logging()` was called at import time, polluting global
  logging and hindering testability. Now called once in `main()`.
- The conversion subprocess could deadlock if stderr filled its pipe.
  stderr is now drained on a dedicated reader thread.
- `-y` was unconditional, risking data loss. Now controlled by config,
  and a hidden temp file is always used regardless.
- FFmpeg refused output written to a `.part` file because it could not
  infer the muxer from the extension. Temp files now carry the real
  container extension (`.mp4`).

### Removed
- Dependency on `beautifulsoup4` (release scraping).
- Dependency on `py7zr` (archive extraction).
- Dependency on `appdirs` (replaced by `core.config`).
- Bundled `7zr.exe` on Windows; extraction uses `zipfile`/`tarfile`.
- The "Default (AppData folder)" output option from the GUI.


[0.1.0] - 2025-05-31
--------------------

Initial Briefcase-generated prototype.

### Added
- Single-file PySide6 GUI.
- Windows-only FFmpeg download via GitHub scraping.
- Simple GPU check via `nvidia-smi` and a name match on
  `ffmpeg -encoders`.
- Basic NVENC/AMF/CPU selection.
- Manual output location with "same as source" and custom directory
  options.