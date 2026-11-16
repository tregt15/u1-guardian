"""Delete old incidents (clip, files and record together).

An incident is deleted when its first trigger is older than `storage.max_age_days`.
0 means keep everything. Protected incidents and ones still recording are never deleted.
"""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta

from guardian.clock import Clock, from_iso
from guardian.config import StorageConfig
from guardian.storage.db import Database
from guardian.storage.paths import incident_dir

log = logging.getLogger(__name__)


def purge_old(storage: StorageConfig, db: Database, clock: Clock) -> int:
    """Delete incidents older than the limit. Returns how many were deleted."""
    days = storage.max_age_days
    if days <= 0:
        return 0
    cutoff = clock.now_utc() - timedelta(days=days)
    root = storage.incidents_dir.resolve()
    deleted = 0
    for row in db.list_incidents():
        if row["protected"] or row["recording_status"] == "recording":
            continue
        try:
            first = from_iso(row["first_trigger_utc"])
        except (TypeError, ValueError):
            continue
        if first >= cutoff:
            continue
        folder = incident_dir(storage, row["id"], first).resolve()
        if folder.is_relative_to(root) and folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        db.delete_incident(row["id"])
        deleted += 1
    if deleted:
        log.info("deleted %d incident(s) older than %d days", deleted, days)
    return deleted
