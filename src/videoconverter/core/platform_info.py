"""OS, architecture, and Apple Silicon detection."""
from __future__ import annotations

import platform
import struct
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum


class OSType(str, Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    UNKNOWN = "unknown"


class ArchType(str, Enum):
    X86_64 = "x86_64"
    ARM64 = "arm64"
    X86 = "x86"
    UNKNOWN = "unknown"


def _normalize_arch(machine: str) -> ArchType:
    m = (machine or "").lower()
    if m in ("x86_64", "amd64"):
        return ArchType.X86_64
    if m in ("arm64", "aarch64"):
        return ArchType.ARM64
    if m in ("i386", "i486", "i586", "i686", "x86"):
        return ArchType.X86
    return ArchType.UNKNOWN


@dataclass(frozen=True)
class PlatformInfo:
    os: OSType
    arch: ArchType
    machine: str
    python_bitness: int
    python_version: str
    is_rosetta: bool
    is_apple_silicon: bool

    @property
    def is_windows(self) -> bool: return self.os is OSType.WINDOWS
    @property
    def is_macos(self) -> bool: return self.os is OSType.MACOS
    @property
    def is_linux(self) -> bool: return self.os is OSType.LINUX

    @property
    def exe_suffix(self) -> str:
        return ".exe" if self.is_windows else ""

    @classmethod
    def detect(cls) -> "PlatformInfo":
        sysname = platform.system().lower()
        if sysname == "windows":
            os_t = OSType.WINDOWS
        elif sysname == "darwin":
            os_t = OSType.MACOS
        elif sysname == "linux":
            os_t = OSType.LINUX
        else:
            os_t = OSType.UNKNOWN

        machine = platform.machine()
        arch = _normalize_arch(machine)
        bitness = struct.calcsize("P") * 8

        is_rosetta = False
        is_apple_silicon = False
        if os_t is OSType.MACOS:
            if arch is ArchType.ARM64:
                is_apple_silicon = True
            # Under Rosetta, sysctl.proc_translated == 1 and platform.machine() == 'x86_64'
            try:
                r = subprocess.run(
                    ["sysctl", "-in", "sysctl.proc_translated"],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0 and r.stdout.strip() == "1":
                    is_rosetta = True
            except Exception:
                pass
            if is_rosetta:
                # The physical machine is Apple Silicon
                is_apple_silicon = True

        return cls(
            os=os_t,
            arch=arch,
            machine=machine,
            python_bitness=bitness,
            python_version=platform.python_version(),
            is_rosetta=is_rosetta,
            is_apple_silicon=is_apple_silicon,
        )

    def describe(self) -> str:
        s = f"{self.os.value} / {self.arch.value}"
        if self.is_rosetta:
            s += " (Rosetta)"
        return s