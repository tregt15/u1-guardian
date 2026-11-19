"""Load and check the YAML config with Pydantic.

Default folders come from platformdirs, so each OS uses its normal place.
Unknown keys are errors, so a typo does not silently do nothing.
Secrets are never read from the file. Use environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from platformdirs import user_config_path, user_data_path
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

APP_NAME = "u1-guardian"


class ConfigError(Exception):
    """The config file is missing, unreadable, or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrinterConfig(_Section):
    host: str = "192.168.1.50"
    # UNVERIFIED on the U1. 7125 is the standard Moonraker default.
    moonraker_port: int = Field(default=7125, ge=1, le=65535)
    # After an incident, read the printer's own logs for that time (read only) and keep them with it.
    fetch_logs: bool = True

    @property
    def api_key(self) -> str:
        return os.environ.get("GUARDIAN_MOONRAKER_KEY", "")


class CameraConfig(_Section):
    # Empty means use the stand-in test video. The real URL is UNVERIFIED until discovery.
    source_url: str = ""
    # Snapshot polling (U1, verified on one printer): GET a JPEG about once a second.
    # On the U1 this is http://<host>/server/files/camera/monitor.jpg. It only changes while
    # the Snapmaker app has the camera view on (touchscreen UNVERIFIED). Empty means do not use snapshot mode.
    snapshot_url: str = ""
    snapshot_poll_s: float = Field(default=0.5, gt=0.05, le=5.0)
    # Let Guardian turn the U1 camera on by itself (sends camera.start_monitor, LAN only, never a stop).
    # off: never. printing: while a print runs or is paused (default). always: also when idle.
    keepalive: Literal["off", "printing", "always"] = "always"
    # A segment longer than segment_length_s + this is treated as hiding a footage gap.
    max_frame_gap_s: float = Field(default=3.0, gt=0.0)
    standin_codec: Literal["h264", "mjpeg"] = "h264"
    segment_length_s: float = Field(default=2.0, ge=1.0, le=10.0)
    latency_offset_ms: int = 0
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"


class BlackboxConfig(_Section):
    pre_trigger_s: float = Field(default=30.0, gt=0)
    post_trigger_s: float = Field(default=15.0, ge=0)
    buffer_mode: Literal["printing", "always"] = "printing"
    max_incident_s: float = Field(default=300.0, gt=0)
    duplicate_window_s: float = Field(default=5.0, ge=0)
    # Also save a browser-playable H.264 MP4 when the camera gives MJPEG or JPEG pictures
    # (browsers cannot play those). The original is kept as clip_original.mkv.
    make_playable_copy: bool = True

    @model_validator(mode="after")
    def _check_lengths(self) -> BlackboxConfig:
        if self.max_incident_s < self.pre_trigger_s + self.post_trigger_s:
            raise ValueError("max_incident_s must be at least pre_trigger_s + post_trigger_s")
        return self


class StorageConfig(_Section):
    data_dir: Path | None = None
    buffer_dir: Path | None = None
    max_total_gb: float = Field(default=5.0, gt=0)
    # Incidents (clips and files) older than this are deleted. 0 keeps everything. Protected ones are kept.
    max_age_days: int = Field(default=7, ge=0)
    max_incidents: int = Field(default=500, gt=0)
    warn_free_gb: float = Field(default=2.0, ge=0)

    def resolved_data_dir(self) -> Path:
        return (self.data_dir or user_data_path(APP_NAME)).expanduser().resolve()

    def resolved_buffer_dir(self) -> Path:
        if self.buffer_dir:
            return self.buffer_dir.expanduser().resolve()
        return self.resolved_data_dir() / "buffer"

    @property
    def db_path(self) -> Path:
        return self.resolved_data_dir() / "guardian.db"

    @property
    def incidents_dir(self) -> Path:
        return self.resolved_data_dir() / "incidents"




def _valid_address(addr: str) -> bool:
    """Plain check: one @, a dot after it, no control characters (stops header tricks).
    Accepts 'name@host.tld' and 'Display Name <name@host.tld>'."""
    if any(ord(ch) < 32 for ch in addr) or len(addr) > 320:
        return False
    if "<" in addr:
        if not addr.endswith(">") or addr.count("<") != 1:
            return False
        addr = addr[addr.index("<") + 1 : -1]
    local, sep, domain = addr.partition("@")
    return bool(sep and local and "." in domain and "@" not in domain and " " not in addr)


Severity = Literal["info", "warning", "critical"]


class EmailConfig(_Section):
    """Email through Resend's HTTPS API. Off by default. Guardian never ships an API key:
    every user makes their own free Resend account and key (see README)."""

    enabled: bool = False
    provider: Literal["resend"] = "resend"  # generic SMTP is not built yet
    # Name of the environment variable that holds YOUR key. RESEND_API_KEY (Resend's own
    # usual name) is also accepted. The key itself must never be put in this file.
    api_key_env: str = "GUARDIAN_RESEND_KEY"
    # Resend's test sender can only deliver to the email address of your own Resend account.
    from_addr: str = "Guardian <onboarding@resend.dev>"
    to_addrs: list[str] = []

    @model_validator(mode="after")
    def _check(self) -> EmailConfig:
        for a in [self.from_addr, *self.to_addrs]:
            if not _valid_address(a):
                raise ValueError(f"not a valid email address: {a!r}")
        if self.enabled and not self.to_addrs:
            raise ValueError("notifications.email.to_addrs needs at least one address when email is enabled")
        return self

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") or os.environ.get("RESEND_API_KEY", "")


class NotificationsConfig(_Section):
    base_url: str = "http://127.0.0.1:8080"  # used for links in messages
    min_severity: Severity = "warning"
    cooldown_s: float = Field(default=60.0, ge=0)  # per event type
    send_on: list[Literal["incident_created", "clip_ready", "diagnosis", "health"]] = [
        "incident_created", "clip_ready", "diagnosis"]
    attach_clip: bool = False  # not built. Clips never leave your network.
    email: EmailConfig = EmailConfig()

    @model_validator(mode="after")
    def _no_attach(self) -> NotificationsConfig:
        if self.attach_clip:
            raise ValueError("attach_clip is not built yet. Messages are text only.")
        return self


LOCAL_BINDS = {"127.0.0.1", "::1", "localhost"}


class ApiConfig(_Section):
    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    token: str = ""  # or env var GUARDIAN_API_TOKEN. Never put a real token in a committed file.

    @property
    def effective_token(self) -> str:
        return os.environ.get("GUARDIAN_API_TOKEN", "") or self.token

    @property
    def is_local(self) -> bool:
        return self.bind in LOCAL_BINDS

    @model_validator(mode="after")
    def _need_token_when_exposed(self) -> ApiConfig:
        if self.enabled and not self.is_local and not self.effective_token:
            raise ValueError(
                "api.bind is not local, so a token is required. Set api.token or the "
                "GUARDIAN_API_TOKEN environment variable."
            )
        return self


class ToolchangerConfig(_Section):
    """Tool change monitoring (spec 5.3). Read only: Guardian only watches the changes."""

    enabled: bool = True
    # A fixed timeout in seconds. Empty means learn it from normal changes.
    timeout_s: float | None = Field(default=None, gt=0)
    # Learned timeout = max(floor, p99 of recent good changes x margin). Real U1 changes took
    # 3 to 5 s (399 changes, one print), so the floor is about 3 times the slowest one.
    timeout_floor_s: float = Field(default=15.0, gt=0)
    timeout_margin: float = Field(default=1.5, ge=1.0)
    learn_count: int = Field(default=20, ge=1)  # good changes needed before learning
    # After a change ends, how long the tool states may take to settle before they count as wrong.
    settle_s: float = Field(default=3.0, gt=0)


# Incident types Guardian may pause a print for. Generic `!!` lines and odd tool states are left
# out on purpose: the printer itself prints "!! Print already paused", and what the tool pin
# values mean is UNVERIFIED.
PAUSABLE_EVENTS = (
    "bed_not_clear", "spaghetti_detected", "residue_detected", "dirty_nozzle_detected",
    "ai_bed_not_clear", "ai_print_failure",
    "toolchange_wrong_tool", "toolchange_error", "toolchange_timeout", "filament_feed_error",
)

AI_MODELS = {  # Ollama model name -> what the settings page shows. Sizes are Ollama's download sizes.
    "qwen2.5vl:7b": "Qwen2.5-VL 7B (about 6 GB, better)",
    "gemma3:4b": "Gemma 3 4B (about 3.3 GB, faster)",
    # UNVERIFIED on the real U1 setup: a smaller model for graphics cards with 6 GB or less.
    "qwen2.5vl:3b": "Qwen2.5-VL 3B (about 3.2 GB, fastest, less exact)",
}


class AiConfig(_Section):
    """Local AI through Ollama (an outside program, like FFmpeg). Off until the user turns it on.
    Nothing is sent to the internet: Guardian only talks to the Ollama address below."""

    enabled: bool = False
    # local: Ollama on this or another computer. openai: any OpenAI-compatible service with an API key.
    # anthropic: Anthropic (Claude) with an API key. With openai or anthropic, pictures and logs go to that company.
    provider: Literal["local", "openai", "anthropic"] = "local"
    remote_url: str = "https://api.openai.com/v1"  # for "openai" (change it for OpenRouter, Groq, Gemini, ...)
    remote_model: str = ""  # the service's model name, for example gpt-4o-mini
    api_key_env: str = "GUARDIAN_AI_KEY"  # name of the environment variable that holds YOUR key
    ollama_url: str = "http://127.0.0.1:11434"
    model: Literal[tuple(AI_MODELS)] = "qwen2.5vl:7b"  # type: ignore[valid-type]
    bed_check: bool = True  # look at the build plate at the start of a print
    print_check: bool = True  # look for spaghetti and other failures during a print
    print_check_every_s: float = Field(default=60.0, ge=15, le=3600)
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    diagnose: Literal["off", "stops", "all"] = "stops"  # write an AI diagnosis after an incident
    timeout_s: float = Field(default=180.0, ge=10, le=1800)

    @model_validator(mode="after")
    def _check_url(self) -> AiConfig:
        for name in ("ollama_url", "remote_url"):
            if not (getattr(self, name).startswith("http://") or getattr(self, name).startswith("https://")):
                raise ValueError(f"ai.{name} must start with http:// or https://")
        if self.enabled and self.provider != "local" and not self.remote_model.strip():
            raise ValueError("ai.remote_model is needed when the AI service is not local")
        if not self.api_key_env.replace("_", "").isalnum():
            raise ValueError("ai.api_key_env must be an environment variable name")
        return self

    @property
    def active_model(self) -> str:
        return self.model if self.provider == "local" else self.remote_model.strip()


class AutopauseConfig(_Section):
    """Pause the print when Guardian finds a problem and the printer did not pause by itself.
    Sends Moonraker's printer.print.pause (Klipper PAUSE). Owner approved, 2026-11-09."""

    enabled: bool = True
    # Wait this long for the printer to pause on its own before Guardian does it.
    grace_s: float = Field(default=10.0, ge=0, le=120)
    triggers: list[Literal[PAUSABLE_EVENTS]] = list(PAUSABLE_EVENTS)  # type: ignore[valid-type]


class ControlsConfig(_Section):
    """Pause, Resume and Stop buttons on the dashboard (owner asked, 2026-11-10). Each sends one standard Moonraker
    request only when a person clicks it and confirms. Guardian itself never resumes or stops a print."""

    enabled: bool = True
    # The camera setup may home the printer and raise the bed (owner asked, 2026-11-11). Only when idle and after the
    # person confirms the plate is empty.
    setup_moves: bool = True
    # Reprint, print a file and upload a file from the dashboard (owner asked, 2026-11-15). Only when idle, and only after
    # a person clicks, confirms and says the plate is empty. Guardian never starts a print by itself.
    print_files: bool = True
    # Before the start command, Guardian looks at the plate with the AI and refuses to start if something is on it or if
    # it cannot tell (owner asked, 2026-11-16). Read only. Turn it off only if you check the plate yourself.
    plate_check: bool = True


class Config(_Section):
    printer: PrinterConfig = PrinterConfig()
    camera: CameraConfig = CameraConfig()
    blackbox: BlackboxConfig = BlackboxConfig()
    toolchanger: ToolchangerConfig = ToolchangerConfig()
    autopause: AutopauseConfig = AutopauseConfig()
    ai: AiConfig = AiConfig()
    controls: ControlsConfig = ControlsConfig()
    storage: StorageConfig = StorageConfig()
    api: ApiConfig = ApiConfig()
    notifications: NotificationsConfig = NotificationsConfig()


def default_config_path() -> Path:
    return user_config_path(APP_NAME) / "config.yaml"


def load_config(path: Path | str | None = None) -> Config:
    """Load config from a YAML file.

    With no path, look in the OS config folder. If that file does not
    exist, return the defaults. If a path is given and missing, that is an error.
    """
    if path is None:
        candidate = default_config_path()
        if not candidate.is_file():
            return Config()
        path = candidate
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"bad YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc
