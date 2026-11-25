"""Atomic JSON state persistence for seen assessments and sent reminders."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import STATE_FILE

STATE_RETENTION_DAYS = 7


def default_state() -> dict[str, Any]:
    """Return an empty initial state structure."""
    return {
        "assessments": {},
        "last_update_id": 0,
        "last_session_alert_at": None,
        "last_scraper_alert_at": None,
    }


def load_state(state_path: Path = STATE_FILE) -> dict[str, Any]:
    """Load state from disk, returning defaults if missing or empty."""
    if not state_path.exists():
        return default_state()
    raw_text = state_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return default_state()
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        return default_state()

    base = default_state()
    base.update(data)
    if not isinstance(base.get("assessments"), dict):
        base["assessments"] = {}
    if not isinstance(base.get("last_update_id"), int):
        base["last_update_id"] = 0
    return base


def save_state(state: dict[str, Any], state_path: Path = STATE_FILE) -> None:
    """Atomically write state.json to disk."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, state_path)


def prune_old_assessments(
    state: dict[str, Any],
    now: datetime,
    tz: ZoneInfo,
    retention_days: int = STATE_RETENTION_DAYS,
) -> None:
    """Remove assessment records whose deadline passed more than retention_days ago."""
    cutoff = now.astimezone(tz) - timedelta(days=retention_days)
    assessments = state.get("assessments", {})
    stale_ids: list[str] = []
    for assessment_id, entry in assessments.items():
        due_raw = entry.get("due_at") if isinstance(entry, dict) else None
        if not due_raw:
            continue
        try:
            due_dt = datetime.fromisoformat(due_raw)
        except ValueError:
            continue
        if due_dt.tzinfo is None:
            due_dt = due_dt.replace(tzinfo=tz)
        if due_dt.astimezone(tz) < cutoff:
            stale_ids.append(assessment_id)

    for assessment_id in stale_ids:
        assessments.pop(assessment_id, None)

