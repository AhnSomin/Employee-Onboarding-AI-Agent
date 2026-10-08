"""Append-only JSONL event log shared by both features (`logs/events.jsonl`).

Never pass meeting text or secrets. Values are limited to short scalars and
long strings are cut, so raw content cannot leak into the log by accident.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import REPO_ROOT, ConfigError, get_settings

LOG_PATH = REPO_ROOT / "logs" / "events.jsonl"
MAX_STR_LEN = 200

# Meeting feature event types (spec section 13).
MEETING_EXTRACTED = "meeting_extracted"
MEETING_APPROVED = "meeting_approved"
ACTIONS_EXECUTED = "actions_executed"
REMINDERS_SENT = "reminders_sent"

_RESERVED_KEYS = {"ts", "event"}

logger = logging.getLogger(__name__)


def log_event(event: str, /, **fields: Any) -> dict[str, Any]:
    """Append one event and return the record that was written.

    A write failure is logged and swallowed: metrics must never break a feature.
    """
    reserved = _RESERVED_KEYS & fields.keys()
    if reserved:
        raise ValueError(f"reserved metric keys: {sorted(reserved)}")

    record: dict[str, Any] = {"ts": _now().isoformat(timespec="seconds"), "event": event}
    record.update({key: _sanitize(value) for key, value in fields.items()})
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("metrics: could not write %s event: %s", event, exc)
    return record


def _now() -> datetime:
    try:
        tz = get_settings().tz
    except ConfigError:
        tz = ZoneInfo("Asia/Seoul")
    return datetime.now(tz)


def _sanitize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_STR_LEN else value[:MAX_STR_LEN] + "…"
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    return _sanitize(str(value))
