"""Job model and states."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .media_probe import MediaInfo


class JobState(str, Enum):
    PENDING = "Pending"
    PROBING = "Probing"
    CONVERTING = "Converting"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    SKIPPED = "Skipped"


@dataclass
class ConversionJob:
    input_path: Path
    output_path: Optional[Path] = None
    state: JobState = JobState.PENDING
    progress: float = 0.0
    error: str = ""
    media_info: Optional[MediaInfo] = None
    backend: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None