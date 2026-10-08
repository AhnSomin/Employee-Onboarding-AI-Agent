"""Complete or cancel approved action items (액션 현황, spec 12).

Done and cancelled items are no longer approved, so the reminder batch skips
them. Only approved items change; repeating a call changes nothing more.
Calendar events and Slack messages are left as they are.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..meeting.models import ActionItem
from ..store.base import StateStore


def _approved(store: StateStore, item_ids: Iterable[str]) -> list[ActionItem]:
    items = (store.get_item(item_id) for item_id in dict.fromkeys(item_ids))
    return [item for item in items if item is not None and item.status == "approved"]


def mark_done(store: StateStore, item_ids: Iterable[str], now: datetime) -> list[ActionItem]:
    return [store.update_item(i.item_id, status="done", completed_at=now) for i in _approved(store, item_ids)]


def mark_cancelled(store: StateStore, item_ids: Iterable[str]) -> list[ActionItem]:
    return [store.update_item(i.item_id, status="cancelled") for i in _approved(store, item_ids)]
