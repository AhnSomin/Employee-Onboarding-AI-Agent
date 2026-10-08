"""State store selection (STATE_BACKEND=sqlite | sheets)."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import NotFoundError, StateStore

__all__ = ["NotFoundError", "StateStore", "StoreUnavailable", "get_store"]


class StoreUnavailable(RuntimeError):
    """The configured backend cannot be used yet. The message is user-facing Korean."""


def get_store(settings: Settings | None = None) -> StateStore:
    settings = settings or get_settings()
    if settings.state_backend == "sqlite":
        from .sqlite_store import SqliteStore

        return SqliteStore(settings.sqlite_path)
    raise StoreUnavailable(
        "Google Sheets 저장소는 아직 준비 중입니다. 지금은 STATE_BACKEND=sqlite로 실행해 주세요."
    )
