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

    @property
    def api_key(self) -> str:
        return os.environ.get("GUARDIAN_MOONRAKER_KEY", "")


class CameraConfig(_Section):
    # Empty means use the stand-in test video. The real URL is UNVERIFIED until discovery.
    source_url: str = ""
    # Snapshot polling (U1, verified on one printer): GET a JPEG about once a second.
    # On the U1 this is http://<host>/server/files/camera/monitor.jpg. It only changes while
    # a viewer (app or screen) has the camera on. Empty means do not use snapshot mode.
    snapshot_url: str = ""
    snapshot_poll_s: float = Field(default=0.5, gt=0.05, le=5.0)
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

    @model_validator(mode="after")
    def _check_lengths(self) -> BlackboxConfig:
        if self.max_incident_s < self.pre_trigger_s + self.post_trigger_s:
            raise ValueError("max_incident_s must be at least pre_trigger_s + post_trigger_s")
        return self


class StorageConfig(_Section):
    data_dir: Path | None = None
    buffer_dir: Path | None = None
    max_total_gb: float = Field(default=5.0, gt=0)
    max_age_days: int = Field(default=90, gt=0)
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


class Config(_Section):
    printer: PrinterConfig = PrinterConfig()
    camera: CameraConfig = CameraConfig()
    blackbox: BlackboxConfig = BlackboxConfig()
    storage: StorageConfig = StorageConfig()


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
