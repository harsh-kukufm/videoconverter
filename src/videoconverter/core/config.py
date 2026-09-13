"""Platform-appropriate application directories and persisted configuration."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "VideoConverter"


def _env(var: str, default: Path) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else default


def user_data_dir() -> Path:
    if sys.platform == "win32":
        return _env("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return _env("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_NAME


def user_log_dir() -> Path:
    if sys.platform == "win32":
        return user_data_dir() / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    base = _env("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return base / APP_NAME / "logs"


def user_config_dir() -> Path:
    if sys.platform == "win32":
        return user_data_dir()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / APP_NAME
    return _env("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME


def user_cache_dir() -> Path:
    if sys.platform == "win32":
        return user_data_dir() / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    return _env("XDG_CACHE_HOME", Path.home() / ".cache") / APP_NAME


def ffmpeg_dir() -> Path:
    return user_data_dir() / "ffmpeg"


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class AppConfig:
    max_resolution: int = 1920
    quality: str = "balanced"           # fast | balanced | quality
    audio_bitrate: str = "192k"
    preferred_backend: str = "auto"     # auto | nvidia | amd | apple | cpu
    allow_fallback: bool = True
    overwrite: bool = False
    log_level: str = "INFO"
    output_option: int = 2              # 1 custom, 2 same-as-source
    custom_output_dir: str = ""
    last_input_dir: str = ""
    window_w: int = 820
    window_h: int = 640

    @classmethod
    def _path(cls) -> Path:
        return user_config_dir() / "config.json"

    @classmethod
    def load(cls) -> "AppConfig":
        p = cls._path()
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)