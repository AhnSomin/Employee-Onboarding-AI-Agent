"""State store selection (STATE_BACKEND=sqlite | sheets)."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import NotFoundError, StateStore

__all__ = ["NotFoundError", "StateStore", "StoreUnavailable", "get_store"]


class StoreUnavailable(RuntimeError):
    """The configured backend cannot be used. The message is user-facing Korean."""


def get_store(settings: Settings | None = None) -> StateStore:
    settings = settings or get_settings()
    if settings.state_backend == "sqlite":
        from .sqlite_store import SqliteStore

        return SqliteStore(settings.sqlite_path)

    from .sheets_store import SheetsStore, SheetsStoreError

    try:
        return SheetsStore.from_settings(settings)
    except SheetsStoreError as exc:
        raise StoreUnavailable(f"Google Sheets 저장소를 열 수 없습니다: {exc}") from None
