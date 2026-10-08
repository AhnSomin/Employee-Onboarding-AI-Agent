"""Google Sheets state store for the demo and the GitHub Actions batch (spec 9.3).

Worksheets `meetings` and `action_items` are created with a header row when
missing. Lists are stored as JSON and dates/times as ISO strings. Every row
carries created_by=onboarding-agent so scripts/reset_demo.py removes only rows
this app wrote. Reads fetch a whole sheet at once and writes are batched to
stay inside the Sheets per-minute quota. There is no locking: two writers at
the same moment can overwrite each other (accepted for the demo).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, TypeVar

from gspread.exceptions import WorksheetNotFound
from gspread.utils import ValueInputOption, rowcol_to_a1
from pydantic import BaseModel

from ..config import ConfigError, Settings, load_service_account_info
from ..meeting.models import CREATED_BY, ActionItem, Meeting
from .base import NotFoundError, apply_update

ModelT = TypeVar("ModelT", bound=BaseModel)

MARKER_COLUMN = "created_by"
MEETING_COLUMNS = [*Meeting.model_fields, MARKER_COLUMN]
ITEM_COLUMNS = [*ActionItem.model_fields, MARKER_COLUMN]
_JSON_FIELDS = {"summary", "decisions", "open_issues", "co_owners", "review_notes"}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsStoreError(RuntimeError):
    """The spreadsheet cannot be used. The message is user-facing Korean."""


def _cell(value: Any) -> str:
    """One JSON-mode value as sheet text (dates and datetimes are already ISO strings)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _to_row(model: BaseModel, columns: list[str]) -> list[str]:
    values = model.model_dump(mode="json")
    values[MARKER_COLUMN] = CREATED_BY
    return [_cell(values.get(column)) for column in columns]


def _from_row(row: dict[str, str], model: type[ModelT]) -> ModelT:
    data: dict[str, Any] = {}
    for name in model.model_fields:
        raw = row.get(name, "")
        if raw == "":
            continue  # let the model default apply (None, [] ...)
        data[name] = json.loads(raw) if name in _JSON_FIELDS else raw
    return model.model_validate(data)


class SheetsStore:
    def __init__(self, spreadsheet: Any, prefix: str = "") -> None:
        """`prefix` gives separate worksheets, e.g. for a live contract test."""
        self._meetings = self._ensure_worksheet(spreadsheet, f"{prefix}meetings", MEETING_COLUMNS)
        self._items = self._ensure_worksheet(spreadsheet, f"{prefix}action_items", ITEM_COLUMNS)
        self.worksheets = (self._meetings, self._items)

    @classmethod
    def from_settings(cls, settings: Settings) -> SheetsStore:
        if not settings.gsheets_spreadsheet_id:
            raise SheetsStoreError("GSHEETS_SPREADSHEET_ID가 설정되지 않았습니다.")
        try:
            info = load_service_account_info(settings)
        except ConfigError as exc:
            raise SheetsStoreError(str(exc)) from None
        if info is None:
            raise SheetsStoreError("GOOGLE_SERVICE_ACCOUNT_JSON이 설정되지 않았습니다.")
        # One instance per process: Streamlit reruns must not re-check headers each time.
        return _cached_store(json.dumps(info, sort_keys=True), settings.gsheets_spreadsheet_id)

    # --- worksheets ---

    @staticmethod
    def _ensure_worksheet(spreadsheet: Any, title: str, columns: list[str]) -> Any:
        try:
            worksheet = spreadsheet.worksheet(title)
        except WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=title, rows=200, cols=len(columns))
        header = (worksheet.get_all_values() or [[]])[0]
        header = [h for h in header if h]
        if header != columns:
            if header and header != columns[: len(header)]:
                raise SheetsStoreError(
                    f"'{title}' 시트의 머리글이 예상과 다릅니다. 시트를 비우거나 머리글을 맞춰 주세요."
                )
            worksheet.update([columns], f"A1:{rowcol_to_a1(1, len(columns))}")  # new columns appended
        return worksheet

    @staticmethod
    def _read(worksheet: Any, columns: list[str]) -> list[dict[str, str]]:
        values = worksheet.get_all_values()
        rows = []
        for raw in values[1:]:
            if not any(raw):
                continue
            padded = (list(raw) + [""] * len(columns))[: len(columns)]
            rows.append(dict(zip(columns, padded, strict=True)))
        return rows

    @staticmethod
    def _row_range(row_number: int, columns: list[str]) -> str:
        return f"{rowcol_to_a1(row_number, 1)}:{rowcol_to_a1(row_number, len(columns))}"

    def _upsert(self, worksheet: Any, columns: list[str], key: str, models: list[BaseModel]) -> None:
        rows = self._read(worksheet, columns)
        positions = {row[key]: index + 2 for index, row in enumerate(rows)}  # header is row 1
        updates, appends = [], []
        for model in models:
            values = _to_row(model, columns)
            position = positions.get(getattr(model, key))
            if position is None:
                appends.append(values)
            else:
                updates.append({"range": self._row_range(position, columns), "values": [values]})
        if updates:
            worksheet.batch_update(updates)
        if appends:
            worksheet.append_rows(appends, value_input_option=ValueInputOption.raw)

    def _update(self, worksheet: Any, columns: list[str], key: str, model: type[ModelT], key_value: str, fields: dict[str, Any]) -> ModelT:
        rows = self._read(worksheet, columns)
        for index, row in enumerate(rows):
            if row[key] == key_value:
                updated = apply_update(_from_row(row, model), fields)
                worksheet.update([_to_row(updated, columns)], self._row_range(index + 2, columns))
                return updated
        raise NotFoundError(key_value)

    # --- meetings ---

    def save_meeting(self, meeting: Meeting) -> None:
        self._upsert(self._meetings, MEETING_COLUMNS, "meeting_id", [meeting])

    def get_meeting(self, meeting_id: str) -> Meeting | None:
        for row in self._read(self._meetings, MEETING_COLUMNS):
            if row["meeting_id"] == meeting_id:
                return _from_row(row, Meeting)
        return None

    def list_meetings(self) -> list[Meeting]:
        return [_from_row(row, Meeting) for row in self._read(self._meetings, MEETING_COLUMNS)]

    def update_meeting(self, meeting_id: str, **fields: Any) -> Meeting:
        return self._update(self._meetings, MEETING_COLUMNS, "meeting_id", Meeting, meeting_id, fields)

    # --- action items ---

    def upsert_items(self, items: list[ActionItem]) -> None:
        if items:
            self._upsert(self._items, ITEM_COLUMNS, "item_id", list(items))

    def get_item(self, item_id: str) -> ActionItem | None:
        for row in self._read(self._items, ITEM_COLUMNS):
            if row["item_id"] == item_id:
                return _from_row(row, ActionItem)
        return None

    def list_items(
        self, *, status: str | None = None, meeting_id: str | None = None
    ) -> list[ActionItem]:
        return [
            _from_row(row, ActionItem)
            for row in self._read(self._items, ITEM_COLUMNS)
            if (status is None or row["status"] == status)
            and (meeting_id is None or row["meeting_id"] == meeting_id)
        ]

    def update_item(self, item_id: str, **fields: Any) -> ActionItem:
        return self._update(self._items, ITEM_COLUMNS, "item_id", ActionItem, item_id, fields)

    # --- maintenance (scripts/reset_demo.py) ---

    def app_row_counts(self) -> dict[str, int]:
        return {
            "meetings": sum(r[MARKER_COLUMN] == CREATED_BY for r in self._read(self._meetings, MEETING_COLUMNS)),
            "action_items": sum(r[MARKER_COLUMN] == CREATED_BY for r in self._read(self._items, ITEM_COLUMNS)),
        }

    def delete_app_rows(self) -> dict[str, int]:
        """Delete rows marked created_by=onboarding-agent; other rows stay."""
        deleted = {}
        for name, worksheet, columns in (
            ("action_items", self._items, ITEM_COLUMNS),
            ("meetings", self._meetings, MEETING_COLUMNS),
        ):
            values = worksheet.get_all_values()
            marker = columns.index(MARKER_COLUMN)
            targets = [
                index + 1
                for index, raw in enumerate(values)
                if index > 0 and len(raw) > marker and raw[marker] == CREATED_BY
            ]
            for row_number in reversed(targets):  # bottom-up keeps row numbers valid
                worksheet.delete_rows(row_number)
            deleted[name] = len(targets)
        return deleted


@lru_cache(maxsize=2)
def _cached_store(info_json: str, spreadsheet_id: str) -> SheetsStore:
    return SheetsStore(_open_spreadsheet(info_json, spreadsheet_id))


def _open_spreadsheet(info_json: str, spreadsheet_id: str) -> Any:
    import gspread
    from gspread.http_client import BackOffHTTPClient

    try:
        client = gspread.service_account_from_dict(
            json.loads(info_json), scopes=SCOPES, http_client=BackOffHTTPClient
        )
        client.set_timeout(30)
        return client.open_by_key(spreadsheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        raise SheetsStoreError(
            "스프레드시트를 찾을 수 없습니다. 서비스 계정 이메일에 편집 권한으로 공유했는지 확인하세요."
        ) from None
    except gspread.exceptions.APIError as exc:
        raise SheetsStoreError(f"Sheets API 오류({exc.response.status_code})") from None
