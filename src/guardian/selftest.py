"""Self-test: checks that what Guardian depends on is still there on the printer. Read only.

Run it after a printer firmware update:

    python -m guardian.selftest --config config.yaml

Guardian also runs it at start and remembers the printer's firmware version. If the version changes,
the dashboard says so, because everything in docs/u1-discovery-notes.md was checked on the old one.
Nothing here sends a command that changes the printer. Starting the camera is not tested here (that
would change printer state). Auto camera shows whether it works during the next print.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from guardian.clock import SystemClock, to_iso
from guardian.config import Config, ConfigError, load_config

STATE_FILE = "printer_firmware.json"
# Objects Guardian reads. All seen on a real U1 (firmware 1.4.1).
REQUIRED_OBJECTS = (
    "print_stats", "toolhead", "virtual_sdcard", "heater_bed", "extruder", "extruder1", "extruder2", "extruder3",
    "machine_state_manager", "filament_feed left", "filament_feed right", "defect_detection", "exception_manager",
)
TESTED_ON = "2.1.0.157_20261028045538"  # first checked on 1.4.1.6_20260608141446, then 2.1.0 (2026-11-10)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


async def run_checks(cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    base = f"http://{cfg.printer.host}:{cfg.printer.moonraker_port}"
    checks: list[Check] = []
    firmware = None
    async with httpx.AsyncClient(base_url=base, timeout=8.0, transport=transport) as c:
        async def get(path: str) -> Any:
            r = await c.get(path)
            r.raise_for_status()
            return r.json().get("result")

        try:
            info = await get("/server/info")
            checks.append(Check("Moonraker answers", True, f"Moonraker {info.get('moonraker_version')}, "
                                                           f"Klipper {info.get('klippy_state')}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("Moonraker answers", False, f"no answer from {base}: {exc}"))
            return _result(checks, None)
        try:
            pinfo = await get("/printer/info")
            firmware = pinfo.get("software_version")
            same = firmware == TESTED_ON
            checks.append(Check("Printer firmware", True, f"{firmware}" + ("" if same else
                                f" (Guardian was checked on {TESTED_ON}. Watch the next print closely.)")))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("Printer firmware", False, f"could not read: {exc}"))
        try:
            names = set((await get("/printer/objects/list")).get("objects", []))
            missing = [o for o in REQUIRED_OBJECTS if o not in names]
            checks.append(Check("Printer objects Guardian reads", not missing,
                                "all there" if not missing else "missing: " + ", ".join(missing)))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("Printer objects Guardian reads", False, f"could not list: {exc}"))
        try:
            files = await get("/server/files/list?root=logs")
            names = {f.get("path") for f in files}
            found = [n for n in ("klippylogs/klippy.log", "klippy.log") if n in names]
            ok = bool(found)
            checks.append(Check("Printer logs readable", ok, f"{found[0]} found" if ok else "klippy.log not listed"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("Printer logs readable", False, f"could not list: {exc}"))
    if cfg.camera.snapshot_url:
        try:
            async with httpx.AsyncClient(timeout=8.0, transport=transport) as c:
                r = await c.get(cfg.camera.snapshot_url)
            ok = r.status_code == 200 and r.content.startswith(b"\xff\xd8")
            checks.append(Check("Camera picture", ok, f"{len(r.content) // 1024} KB JPEG" if ok else f"HTTP {r.status_code}"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("Camera picture", False, str(exc)))
    if cfg.ai.enabled and cfg.ai.provider != "local":
        from guardian.ai.remote import RemoteAI, api_key
        ok, msg = await RemoteAI(cfg.ai.provider, cfg.ai.remote_url, api_key(cfg.ai.api_key_env), transport).check(
            cfg.ai.active_model)
        checks.append(Check("AI service", ok, f"{cfg.ai.provider}, model {cfg.ai.active_model}: {msg}"))
    elif cfg.ai.enabled:
        from guardian.ai.ollama import Ollama
        o = Ollama(cfg.ai.ollama_url, transport)
        version = await o.version()
        has = bool(version) and await o.has_model(cfg.ai.model)
        checks.append(Check("Local AI", has, f"Ollama {version}, model {cfg.ai.model} "
                            + ("ready" if has else "not downloaded") if version else f"Ollama not found at {cfg.ai.ollama_url}"))
    return _result(checks, firmware)


def _result(checks: list[Check], firmware: str | None) -> dict[str, Any]:
    return {"ran_utc": to_iso(SystemClock().now_utc()), "ok": all(c.ok for c in checks), "firmware": firmware,
            "checks": [asdict(c) for c in checks]}


def remember_firmware(data_dir: Path, firmware: str | None) -> str | None:
    """Store the firmware version. Returns the old version if it changed, else None."""
    if not firmware:
        return None
    path = data_dir / STATE_FILE
    old = None
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8")).get("firmware")
        except ValueError:
            old = None
    if old != firmware:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"firmware": firmware, "previous": old}), encoding="utf-8")
    return old if old and old != firmware else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path)
    args = ap.parse_args(argv)
    try:
        from guardian.settings import load_overrides
        cfg = load_overrides(load_config(args.config))
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    result = asyncio.run(run_checks(cfg))
    for c in result["checks"]:
        print(f"[{'ok ' if c['ok'] else 'BAD'}] {c['name']}: {c['detail']}")
    print("All good." if result["ok"] else "Some checks failed. See above.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
