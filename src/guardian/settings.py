"""Settings you can change from the dashboard.

Only the fields in FIELDS can be changed. Changes are checked with the normal config rules, saved to
`settings.yaml` in the data folder (config.yaml is never rewritten), and applied right away.
On start, settings.yaml is laid over config.yaml. No secrets are ever editable here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from guardian.config import AI_MODELS, PAUSABLE_EVENTS, Config

log = logging.getLogger(__name__)

SETTINGS_FILE = "settings.yaml"

# key, label, kind, help, extra. Kinds: choice, bool, number, number_or_empty, multi, emails.
FIELDS: list[dict[str, Any]] = [
    {"key": "camera.keepalive", "group": "Camera", "label": "Auto camera", "kind": "choice",
     "options": {"off": "Off", "printing": "During prints", "always": "Always"},
     "help": "Guardian turns the U1 camera on by itself (LAN only). It never turns it off. "
             "Always also keeps recent footage while the printer is idle."},
    {"key": "controls.enabled", "group": "Printer controls", "label": "Show Pause, Resume and Stop buttons",
     "kind": "bool", "help": "Buttons on the dashboard. Each asks you to confirm before anything is sent."},
    {"key": "autopause.enabled", "group": "Auto pause", "label": "Pause the print on a problem", "kind": "bool",
     "help": "If Guardian finds a problem below and the printer did not pause by itself, Guardian pauses it."},
    {"key": "autopause.grace_s", "group": "Auto pause", "label": "Wait before pausing (seconds)", "kind": "number",
     "min": 0, "max": 120, "help": "Time the printer gets to pause by itself first."},
    {"key": "autopause.triggers", "group": "Auto pause", "label": "Pause for", "kind": "multi",
     "options": {
         "bed_not_clear": "Build plate not clear (printer AI)",
         "spaghetti_detected": "Spaghetti (printer AI)",
         "residue_detected": "Residue (printer AI)",
         "dirty_nozzle_detected": "Dirty nozzle (printer AI)",
         "ai_bed_not_clear": "Build plate not clear (Guardian AI)",
         "ai_print_failure": "Print failure such as spaghetti (Guardian AI)",
         "toolchange_wrong_tool": "Tool change: wrong tool",
         "toolchange_error": "Tool change: printer error",
         "toolchange_timeout": "Tool change: stuck",
         "filament_feed_error": "Filament feed error",
     }, "help": "Problems that can pause a print."},
    {"key": "ai.enabled", "group": "AI", "label": "Use AI", "kind": "bool",
     "help": "Local AI needs Ollama (free) and a downloaded model. Online AI needs your API key. See the AI box below."},
    {"key": "ai.provider", "group": "AI", "label": "Where the AI runs", "kind": "choice",
     "options": {"local": "On my computer (Ollama, private)", "openai": "Online service, OpenAI-compatible (API key)",
                 "anthropic": "Online service, Anthropic Claude (API key)"},
     "help": "Online services need your own API key and get the camera pictures and printer logs for each check."},
    {"key": "ai.remote_model", "group": "AI", "label": "Online model name", "kind": "text",
     "help": "Only for online services. The model name from your provider, for example gpt-4o-mini."},
    {"key": "ai.remote_url", "group": "AI", "label": "OpenAI-compatible address", "kind": "text",
     "help": "Only for OpenAI-compatible services. OpenAI is https://api.openai.com/v1. OpenRouter, Groq and others "
             "give their own address."},
    {"key": "ai.api_key_env", "group": "AI", "label": "API key environment variable", "kind": "text",
     "help": "Put your key in this environment variable or in the .env file next to config.yaml. "
             "The key itself is never saved by Guardian."},
    {"key": "ai.model", "group": "AI", "label": "Local model", "kind": "choice", "options": dict(AI_MODELS),
     "help": "Bigger is better but slower. Needs a graphics card or a fast computer. Too big for a Raspberry Pi."},
    {"key": "ai.ollama_url", "group": "AI", "label": "Ollama address", "kind": "text",
     "help": "Leave as is if Ollama runs on this computer. A Pi can use Ollama on another computer."},
    {"key": "ai.bed_check", "group": "AI", "label": "Check the build plate when a print starts", "kind": "bool",
     "help": ""},
    {"key": "ai.print_check", "group": "AI", "label": "Check for failures during a print", "kind": "bool",
     "help": "Spaghetti, a part that broke loose, a blob on the nozzle."},
    {"key": "ai.print_check_every_s", "group": "AI", "label": "Check every (seconds)", "kind": "number",
     "min": 15, "max": 3600, "help": ""},
    {"key": "ai.min_confidence", "group": "AI", "label": "How sure the AI must be (0 to 1)", "kind": "number",
     "min": 0, "max": 1, "help": "Higher means fewer false alarms but more missed problems."},
    {"key": "ai.diagnose", "group": "AI", "label": "Write a diagnosis after", "kind": "choice",
     "options": {"off": "Never", "stops": "Prints that stop or fail", "all": "Every incident"},
     "help": "The AI reads the timeline, logs and pictures and guesses the cause. Sent by email if email is on."},
    {"key": "toolchanger.enabled", "group": "Tool changes", "label": "Watch tool changes", "kind": "bool", "help": ""},
    {"key": "toolchanger.timeout_s", "group": "Tool changes", "label": "Stuck after (seconds)",
     "kind": "number_or_empty", "min": 1, "max": 600,
     "help": "Leave empty to learn it from your printer's normal tool changes."},
    {"key": "toolchanger.timeout_floor_s", "group": "Tool changes", "label": "Never call stuck sooner than (seconds)",
     "kind": "number", "min": 1, "max": 600, "help": "Used while learning, and as the lowest learned value."},
    {"key": "printer.fetch_logs", "group": "Recording", "label": "Keep the printer's logs with each incident",
     "kind": "bool", "help": "Reads the printer's own logs for that time (read only)."},
    {"key": "blackbox.pre_trigger_s", "group": "Recording", "label": "Seconds before a problem", "kind": "number",
     "min": 1, "max": 600, "help": "Footage saved from before the problem."},
    {"key": "blackbox.post_trigger_s", "group": "Recording", "label": "Seconds after a problem", "kind": "number",
     "min": 0, "max": 600, "help": "Footage saved after the problem."},
    {"key": "notifications.email.enabled", "group": "Email alerts", "label": "Send email alerts", "kind": "bool",
     "help": "Needs your own Resend API key in an environment variable (see the README)."},
    {"key": "notifications.email.to_addrs", "group": "Email alerts", "label": "Send to", "kind": "emails",
     "help": "One address per line."},
    {"key": "notifications.min_severity", "group": "Email alerts", "label": "Lowest severity to send", "kind": "choice",
     "options": {"info": "Info", "warning": "Warning", "critical": "Critical"}, "help": ""},
    {"key": "notifications.cooldown_s", "group": "Email alerts", "label": "Quiet time per alert type (seconds)",
     "kind": "number", "min": 0, "max": 86400, "help": ""},
]
KEYS = frozenset(f["key"] for f in FIELDS)
_TRIGGERS = next(f for f in FIELDS if f["key"] == "autopause.triggers")
if set(_TRIGGERS["options"]) != set(PAUSABLE_EVENTS):  # keep the page in step with the config
    raise RuntimeError("autopause.triggers options do not match PAUSABLE_EVENTS")


class SettingsError(ValueError):
    pass


def settings_path(cfg: Config) -> Path:
    return cfg.storage.resolved_data_dir() / SETTINGS_FILE


def _get(obj: Any, key: str) -> Any:
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj


def _set_dict(d: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def current(cfg: Config) -> dict[str, Any]:
    out = {}
    for f in FIELDS:
        v = _get(cfg, f["key"])
        out[f["key"]] = list(v) if isinstance(v, list) else v
    return out


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and key not in KEYS:
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _validated(cfg: Config, changes: dict[str, Any]) -> Config:
    unknown = set(changes) - KEYS
    if unknown:
        raise SettingsError(f"these settings cannot be changed here: {sorted(unknown)}")
    raw = cfg.model_dump()
    for key, value in changes.items():
        _set_dict(raw, key, value)
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        msgs = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise SettingsError("; ".join(msgs)) from exc


def _apply_in_place(live: Config, new: Config, keys: list[str]) -> None:
    """Copy values into the live config objects, so every part holding a section sees them."""
    for key in keys:
        parent_path, _, leaf = key.rpartition(".")
        parent = _get(live, parent_path)
        value = _get(new, key)
        object.__setattr__(parent, leaf, list(value) if isinstance(value, list) else value)


def load_overrides(cfg: Config) -> Config:
    """Lay settings.yaml over the loaded config. A bad file is logged and ignored, never fatal."""
    path = settings_path(cfg)
    if not path.is_file():
        return cfg
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        changes = {k: v for k, v in _flatten(raw).items() if k in KEYS}
        new = _validated(cfg, changes)
    except (OSError, yaml.YAMLError, SettingsError) as exc:
        log.error("ignoring %s, it could not be used: %s", path, exc)
        return cfg
    _apply_in_place(cfg, new, list(changes))
    log.info("settings from %s: %s", path, sorted(changes))
    return cfg


def save(cfg: Config, changes: dict[str, Any]) -> list[str]:
    """Check, save and apply. Returns the keys that changed. Raises SettingsError."""
    new = _validated(cfg, changes)
    changed = [k for k in changes if _get(new, k) != _get(cfg, k)]
    path = settings_path(cfg)
    existing: dict = {}
    if path.is_file():
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            existing = {}
    flat = {k: v for k, v in _flatten(existing).items() if k in KEYS}
    for k in changes:
        v = _get(new, k)
        flat[k] = list(v) if isinstance(v, list) else v
    nested: dict = {}
    for k, v in sorted(flat.items()):
        _set_dict(nested, k, v)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Written by the Guardian dashboard. Laid over config.yaml.\n"
                   + yaml.safe_dump(nested, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    _apply_in_place(cfg, new, list(changes))
    return changed
