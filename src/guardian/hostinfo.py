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


# ---- Ollama install help (the only OS specific part of the local AI) ----

OLLAMA_LINUX_CMD = "curl -fsSL https://ollama.com/install.sh | sh"


def ollama_install_plan() -> dict:
    """How Ollama can be installed on this computer.

    Windows with winget: Guardian can do it (winget is Microsoft's package manager and checks the
    download's hash). Linux and macOS: Ollama's installer needs admin rights, which Guardian does
    not have when it runs as a service, so the user gets the command to run."""
    system = platform.system()
    if system == "Windows" and shutil.which("winget"):
        return {"can_install": True, "method": "winget",
                "command": ["winget", "install", "--id", "Ollama.Ollama", "--exact", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
                "manual": "Or install it yourself from https://ollama.com/download"}
    if system == "Windows":
        return {"can_install": False, "method": "manual", "command": None,
                "manual": "winget was not found. Install Ollama from https://ollama.com/download"}
    if system == "Linux":
        small = (mem_available_mb() or 0) < 3000 and platform.machine() in ("aarch64", "armv7l")
        note = (" This computer looks like a small Raspberry Pi. The AI models need more memory than it has. "
                "Install Ollama on a bigger computer instead and put its address in the Ollama address setting.") if small else ""
        return {"can_install": False, "method": "manual", "command": None,
                "manual": f"Run this in a terminal (it asks for your password): {OLLAMA_LINUX_CMD}.{note}"}
    return {"can_install": False, "method": "manual", "command": None,
            "manual": "Install Ollama from https://ollama.com/download"}


def ollama_start_command() -> list[str] | None:
    """A command that starts Ollama if it is installed but not running, or None."""
    if platform.system() == "Windows":
        app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
        if app.is_file():
            return [str(app)]
    exe = shutil.which("ollama")
    return [exe, "serve"] if exe else None


def start_detached(cmd: list[str]) -> None:
    """Start a program that keeps running after Guardian stops."""
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
    else:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
