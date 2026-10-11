"""Google Calendar access through the service account (spec 8.5).

Writes only to GCAL_CALENDAR_ID, a calendar a person created and shared with
the service account ("일정 변경" permission); events in the service account's
own calendar would be invisible to people. Never adds attendees: a service
account cannot invite people without domain-wide delegation, so Slack does
the notifying.
"""

from __future__ import annotations

from typing import Any, Literal

from ..config import ConfigError, Settings, load_service_account_info
from ..meeting.models import CREATED_BY

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TIMEOUT_SEC = 30
NUM_RETRIES = 2  # googleapiclient backs off exponentially on 429 and 5xx

SHARE_HINT = "캘린더를 서비스 계정 이메일에 '일정 변경' 권한으로 공유했는지 확인하세요."


class CalendarError(RuntimeError):
    """A calendar call failed. The message is user-facing Korean."""


def _friendly(exc: Exception) -> CalendarError:
    status = getattr(exc, "status_code", None)
    if status in (403, 404):
        return CalendarError(f"캘린더에 접근할 수 없습니다({status}). {SHARE_HINT}")
    if status == 401:
        return CalendarError("서비스 계정 인증에 실패했습니다. 키 파일을 확인하세요.")
    if status is not None:
        return CalendarError(f"Calendar API 오류({status})")
    return CalendarError(f"캘린더에 연결하지 못했습니다 ({type(exc).__name__}).")


class CalendarClient:
    def __init__(self, service: Any, calendar_id: str) -> None:
        self._events = service.events()
        self.calendar_id = calendar_id

    @classmethod
    def from_settings(cls, settings: Settings) -> CalendarClient:
        try:
            info = load_service_account_info(settings)
        except ConfigError as exc:
            raise CalendarError(str(exc)) from None
        missing = [
            name
            for name, value in (("GCAL_CALENDAR_ID", settings.gcal_calendar_id), ("GOOGLE_SERVICE_ACCOUNT_JSON", info))
            if not value
        ]
        if missing:
            raise CalendarError("설정이 필요합니다: " + ", ".join(missing))
        if settings.gcal_calendar_id in ("primary", info.get("client_email")):
            raise CalendarError("서비스 계정 자신의 캘린더는 사람에게 보이지 않습니다. 공유받은 캘린더 ID를 넣으세요.")

        import google_auth_httplib2
        import httplib2
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=TIMEOUT_SEC))
        service = build("calendar", "v3", http=http, cache_discovery=False)
        return cls(service, settings.gcal_calendar_id)

    def _execute(self, request: Any) -> dict[str, Any]:
        try:
            return request.execute(num_retries=NUM_RETRIES)
        except Exception as exc:
            raise _friendly(exc) from None

    def insert_event(self, body: dict[str, Any]) -> tuple[str, Literal["created", "skipped_existing"]]:
        """Create the event. A 409 on the deterministic id means it already exists."""
        try:
            created = self._events.insert(calendarId=self.calendar_id, body=body).execute(
                num_retries=NUM_RETRIES
            )
            return created["id"], "created"
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise _friendly(exc) from None
        existing = self._execute(self._events.get(calendarId=self.calendar_id, eventId=body["id"]))
        if existing.get("status") == "cancelled":
            # Deleted earlier (e.g. by reset_demo); ids cannot be reused, so restore it.
            restored = self._execute(
                self._events.update(
                    calendarId=self.calendar_id, eventId=body["id"], body={**body, "status": "confirmed"}
                )
            )
            return restored["id"], "created"
        return existing["id"], "skipped_existing"

    def delete_event(self, event_id: str) -> bool:
        """Delete an event. Returns False when it is already gone."""
        try:
            self._events.delete(calendarId=self.calendar_id, eventId=event_id).execute(
                num_retries=NUM_RETRIES
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) in (404, 410):
                return False
            raise _friendly(exc) from None
        return True

    def list_app_events(self) -> list[dict[str, Any]]:
        """Events this app created (extendedProperties.private.created_by)."""
        events: list[dict[str, Any]] = []
        page_token = None
        while True:
            page = self._execute(
                self._events.list(
                    calendarId=self.calendar_id,
                    privateExtendedProperty=f"created_by={CREATED_BY}",
                    singleEvents=True,
                    showDeleted=False,
                    maxResults=250,
                    pageToken=page_token,
                )
            )
            events.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return events
