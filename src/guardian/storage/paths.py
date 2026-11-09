"""Where an incident lives on disk."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from guardian.config import StorageConfig


def incident_dir(storage: StorageConfig, incident_id: int, first_trigger: datetime) -> Path:
    """incidents/2026/11/incident_0001 (UTC year and month of the first trigger)."""
    return (storage.incidents_dir / f"{first_trigger:%Y}" / f"{first_trigger:%m}"
            / f"incident_{incident_id:04d}")
