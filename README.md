# Video Converter

A cross-platform desktop application that downsizes large video files by
re-encoding them to **HEVC (H.265)** using the best available hardware
encoder — NVIDIA NVENC, AMD AMF/VAAPI, Apple VideoToolbox — or falling
back to CPU encoding when no GPU backend is usable.

Built with [Python](https://www.python.org/) + [PySide6](https://doc.qt.io/qtforpython-6/)
and packaged with [Briefcase](https://briefcase.readthedocs.io/).

---

## Features

- **Hardware-accelerated HEVC encoding** with automatic, validated
  backend selection:
  - NVIDIA NVENC (Windows, Linux)
  - AMD AMF (Windows), AMD VAAPI (Linux)
  - Intel VAAPI (Linux)
  - Apple VideoToolbox (macOS Intel and Apple Silicon)
  - CPU fallback (`libx265`) on every platform
- **Automatic downscaling** of 4K and larger sources to a configurable
  maximum resolution (default 1920 px on the longest side). Aspect ratio
  and even-dimension alignment are preserved. No upscaling.
- **Quality-targeted encoding** (CRF / CQ / QP) rather than a fixed
  bitrate — output size depends on content, but typical 4K → 1080p
  reductions are 70–95 %.
- **Managed FFmpeg installation.** The app downloads, validates, and
  atomically installs the correct FFmpeg build for the current
  OS/architecture. A previous known-good install is preserved for
  rollback.
- **Update detection.** The Setup button only offers *Update* when a
  genuinely newer build is available at the source; otherwise it
  offers *Setup* (first install) or *Reinstall*.
- **Robust batch conversion** with per-file state, overall progress,
  cancellation, and cleanup of partial output.
- **Rotating persistent logs** in a platform-appropriate directory.
- **Cross-platform**: Windows x64/ARM64, macOS Intel/Apple Silicon,
  Linux x64/ARM64.

---

## Requirements

- Python **3.11+**
- OS: Windows 10/11, macOS 12+, or a modern Linux distribution
- Internet access on first launch (to fetch FFmpeg) unless FFmpeg is
  already on `PATH`

Python dependencies (see `requirements.txt`):

```
PySide6-Essentials~=6.8
requests>=2.31
```

---

## Installation & running from source

```bash
git clone https://github.com/Th3-A6add0n/videoconverter.git
cd videoconverter

python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt
python -m videoconverter
```

On first launch the app will:

1. Detect the OS, CPU architecture, and available GPU(s).
2. Look for FFmpeg in the managed directory
   (`.../VideoConverter/ffmpeg/`), then on `PATH`.
3. If not found, offer to download the correct build for your platform.

No manual FFmpeg installation is required.

---

## Packaging with Briefcase

```bash
pip install briefcase

# Windows
briefcase create windows
briefcase build  windows
briefcase package windows

# macOS  (run on the target architecture; the .app is native)
briefcase create macOS
briefcase build  macOS
briefcase package macOS

# Linux
briefcase create linux system   # or .appimage / .flatpak
briefcase build  linux system
briefcase package linux system
```

The packaged installers are written to `dist/`.

---

## Where files live

| Purpose | Windows | macOS | Linux |
|---|---|---|---|
| Data (managed FFmpeg) | `%LOCALAPPDATA%\VideoConverter` | `~/Library/Application Support/VideoConverter` | `$XDG_DATA_HOME/VideoConverter` |
| Logs | `%LOCALAPPDATA%\VideoConverter\logs` | `~/Library/Logs/VideoConverter` | `$XDG_STATE_HOME/VideoConverter/logs` |
| Config | `%LOCALAPPDATA%\VideoConverter\config.json` | `~/Library/Preferences/VideoConverter/config.json` | `$XDG_CONFIG_HOME/VideoConverter/config.json` |
| Cache | `%LOCALAPPDATA%\VideoConverter\cache` | `~/Library/Caches/VideoConverter` | `$XDG_CACHE_HOME/VideoConverter` |

Logs rotate at 4 MiB, keeping 5 backups.

---

## Configuration

`config.json` in the platform config directory supports:

```jsonc
{
  "max_resolution": 1920,       // longest side of output, in pixels
  "quality": "balanced",        // "fast" | "balanced" | "quality"
  "audio_bitrate": "192k",      // used only when audio is re-encoded
  "preferred_backend": "auto",  // "auto" | "nvidia" | "amd" | "apple" | "cpu"
  "allow_fallback": true,       // fall back to CPU if HW fails
  "overwrite": false,           // overwrite existing outputs
  "log_level": "INFO",          // DEBUG | INFO | WARNING | ERROR
  "output_option": 2,           // 1 = custom dir, 2 = alongside source
  "custom_output_dir": ""
}
```

---

## Hardware acceleration matrix

| Platform | Arch | Backend used (in preference order) |
|---|---|---|
| Windows 10/11 | x86_64 | NVENC → AMF → CPU |
| Windows 10/11 | arm64 | CPU |
| macOS 12+ | x86_64 (Intel) | VideoToolbox → CPU |
| macOS 12+ | arm64 (Apple Silicon) | VideoToolbox → CPU |
| Linux | x86_64 / arm64 | NVENC → AMD/Intel VAAPI → CPU |

The selected backend is only reported as active after a real one-frame
null-encode test confirms the encoder actually works. If a GPU exists
but its encoder is unusable, the app logs the reason and falls back.

---

## Testing

```bash
pip install pytest
pytest -q tests/
```

The tests exercise pure logic only (scaling, parsing, safe archive
extraction, backend model). No GPU, FFmpeg, or network access is
required.

---

## Security notes

- Archive extraction is guarded against path traversal (zip-slip and
  tar variants), including Windows drive-letter tricks and symlinks
  that would escape the extraction root.
- Downloads use a size cap, streamed writes, and atomic
  `.part` → final rename.
- The FFmpeg binary is validated (`ffmpeg -version`) *before* it is
  swapped into place; the previous install is kept as a rollback
  target until the new one passes validation.
- Subprocesses are launched with argument arrays (no `shell=True`),
  with timeouts, and are killed as a whole tree on cancellation.
- Output is written to a hidden temp file with the correct container
  extension and atomically renamed on success; malformed or truncated
  output never replaces the target.

---

## License

MIT — see [`LICENSE`](LICENSE).

## Author

**Th3 A6add0n** — <th3.a6add0n@gmail.com>