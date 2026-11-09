"""Installs Ollama when the user asks for it from the Settings page.

The platform details (which command, how to start Ollama) live in guardian/hostinfo.py. This module
only runs the plan, keeps progress for the page, and waits until Ollama answers. Never raises.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from guardian import hostinfo

log = logging.getLogger(__name__)

WAIT_FOR_START_S = 90.0
ALREADY_INSTALLED_RE = re.compile(r"already installed|no newer package|no available upgrade", re.I)


@dataclass
class InstallState:
    running: bool = False
    step: str | None = None
    done: bool = False
    error: str | None = None
    output: deque = field(default_factory=lambda: deque(maxlen=15))

    def as_dict(self) -> dict[str, Any]:
        return {"running": self.running, "step": self.step, "done": self.done, "error": self.error,
                "output": list(self.output)}


class OllamaInstaller:
    def __init__(self, version: Callable[[], Awaitable[str | None]], *,
                 plan: Callable[[], dict] = hostinfo.ollama_install_plan,
                 start_cmd: Callable[[], list[str] | None] = hostinfo.ollama_start_command,
                 start: Callable[[list[str]], None] = hostinfo.start_detached,
                 wait_s: float = WAIT_FOR_START_S, poll_s: float = 2.0) -> None:
        self._version = version  # async: Ollama's version, or None
        self._plan, self._start_cmd, self._start = plan, start_cmd, start
        self.wait_s, self.poll_s = wait_s, poll_s
        self.state = InstallState()

    def plan(self) -> dict:
        p = self._plan()
        return {k: v for k, v in p.items() if k != "command"}

    async def install(self) -> None:
        st = self.state = InstallState(running=True, step="starting")
        try:
            plan = self._plan()
            if not plan.get("can_install") or not plan.get("command"):
                st.error = plan.get("manual") or "Guardian cannot install Ollama on this computer."
                return
            if await self._version():
                st.step, st.done = "Ollama is already running", True
                return
            st.step = "installing Ollama (this can take a few minutes)"
            log.warning("installing Ollama at the user's request: %s", " ".join(plan["command"]))
            code, text = await self._run(plan["command"])
            if code != 0 and not ALREADY_INSTALLED_RE.search(text):
                st.error = f"The installer stopped with code {code}. {plan.get('manual', '')}".strip()
                return
            st.step = "waiting for Ollama to start"
            if await self._wait():
                st.step, st.done = "Ollama is installed and running", True
                return
            cmd = self._start_cmd()
            if cmd:
                st.step = "starting Ollama"
                self._start(cmd)
                if await self._wait():
                    st.step, st.done = "Ollama is installed and running", True
                    return
            st.error = "Ollama was installed but did not start. Start Ollama from the Start menu, then reload this page."
        except Exception as exc:  # noqa: BLE001
            st.error = f"Install failed: {type(exc).__name__}: {exc}"
            log.exception("Ollama install failed")
        finally:
            st.running = False

    async def _run(self, cmd: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        all_text: list[str] = []
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            all_text.append(text)
            for part in re.split(r"[\r\n]+", text):
                part = part.strip()
                if part and not set(part) <= set("-\|/ █▒"):  # skip spinner and progress bar drawing
                    self.state.output.append(part[:200])
        return await proc.wait(), "".join(all_text)

    async def _wait(self) -> bool:
        loops = max(1, int(self.wait_s / self.poll_s))
        for _ in range(loops):
            if await self._version():
                return True
            await asyncio.sleep(self.poll_s)
        return False
