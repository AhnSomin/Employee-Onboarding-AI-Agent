"""State store contract shared by the SQLite and Google Sheets backends (spec 9.2)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..meeting.models import ActionItem, Meeting

ModelT = TypeVar("ModelT", bound=BaseModel)


class NotFoundError(KeyError):
    """The requested meeting or action item does not exist."""


class StateStore(Protocol):
    def save_meeting(self, meeting: Meeting) -> None: ...

    def get_meeting(self, meeting_id: str) -> Meeting | None: ...

    def list_meetings(self) -> list[Meeting]: ...

    def update_meeting(self, meeting_id: str, **fields: Any) -> Meeting: ...

    def upsert_items(self, items: list[ActionItem]) -> None: ...

    def get_item(self, item_id: str) -> ActionItem | None: ...

    def list_items(
        self, *, status: str | None = None, meeting_id: str | None = None
    ) -> list[ActionItem]: ...

    def update_item(self, item_id: str, **fields: Any) -> ActionItem: ...


def apply_update(model: ModelT, fields: Mapping[str, Any]) -> ModelT:
    """Return a re-validated copy of `model` with `fields` applied.

    Unknown fields are rejected, so both backends enforce the same rules.
    """
    unknown = set(fields) - set(type(model).model_fields)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    data = model.model_dump()
    data.update(fields)
    return type(model).model_validate(data)
