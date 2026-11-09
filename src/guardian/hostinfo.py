"""Small host helper: the only place that reads OS specific files.

Everything returns None when the OS does not offer it (for example on Windows),
and never raises. On a Raspberry Pi these show CPU temperature and free memory.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


def cpu_temp_c() -> float | None:
    """CPU temperature from the Linux thermal zone, if present."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return round(int(raw) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def mem_available_mb() -> int | None:
    """Available memory from /proc/meminfo (Linux only)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def load_avg() -> list[float] | None:
    try:
        import os

        return [round(x, 2) for x in os.getloadavg()]  # not on Windows
    except (OSError, AttributeError):
        return None


def disk_free_gb(path: Path) -> float | None:
    """Free space of the disk that holds `path` (walks up to a folder that exists)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return round(shutil.disk_usage(p).free / 1e9, 2)
    except OSError:
        return None


def folder_size_gb(path: Path) -> float:
    total = 0
    try:
        for f in Path(path).rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return round(total / 1e9, 3)


def low_priority_kwargs() -> dict:
    """Extra subprocess arguments that run a job at low CPU priority (for the one video encode
    at incident time, so it never competes with capture on a small Pi)."""
    if os.name == "nt":
        return {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS}
    return {"preexec_fn": lambda: os.nice(10)}


def describe() -> dict:
    return {"system": platform.system(), "machine": platform.machine(),
            "python": platform.python_version()}
