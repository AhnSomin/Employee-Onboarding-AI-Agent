"""CalendarClient against a scripted stand-in for the googleapiclient service."""

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from onboarding_agent.config import Settings
from onboarding_agent.integrations.gcal import CalendarClient, CalendarError


def http_error(status: int) -> HttpError:
    return HttpError(Response({"status": status}), b'{"error": {"message": "x"}}')


class Request:
    def __init__(self, outcome):
        self.outcome = outcome

    def execute(self, num_retries=0):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeEvents:
    def __init__(self, **outcomes):
        self.outcomes = {name: list(values) for name, values in outcomes.items()}
        self.calls = []

    def _next(self, name, kwargs):
        self.calls.append((name, kwargs))
        return Request(self.outcomes[name].pop(0))

    def insert(self, **kwargs):
        return self._next("insert", kwargs)

    def get(self, **kwargs):
        return self._next("get", kwargs)

    def update(self, **kwargs):
        return self._next("update", kwargs)

    def delete(self, **kwargs):
        return self._next("delete", kwargs)

    def list(self, **kwargs):
        return self._next("list", kwargs)


class FakeService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


BODY = {"id": "abc123", "summary": "[액션] 할 일 (김민준)"}


def client(**outcomes):
    events = FakeEvents(**outcomes)
    return CalendarClient(FakeService(events), "demo@group.calendar.google.com"), events


def test_insert_creates_event():
    calendar, events = client(insert=[{"id": "abc123"}])
    assert calendar.insert_event(BODY) == ("abc123", "created")
    assert events.calls[0][1]["calendarId"] == "demo@group.calendar.google.com"


def test_conflict_reuses_existing_event():
    calendar, events = client(insert=[http_error(409)], get=[{"id": "abc123", "status": "confirmed"}])
    assert calendar.insert_event(BODY) == ("abc123", "skipped_existing")
    assert [name for name, _ in events.calls] == ["insert", "get"]


def test_conflict_with_deleted_event_restores_it():
    calendar, events = client(
        insert=[http_error(409)], get=[{"id": "abc123", "status": "cancelled"}], update=[{"id": "abc123"}]
    )
    assert calendar.insert_event(BODY) == ("abc123", "created")
    assert events.calls[-1][1]["body"]["status"] == "confirmed"


@pytest.mark.parametrize("status", [403, 404])
def test_permission_errors_explain_sharing(status):
    calendar, _ = client(insert=[http_error(status)])
    with pytest.raises(CalendarError, match="일정 변경"):
        calendar.insert_event(BODY)


def test_delete_ignores_missing_events_and_list_pages_through():
    calendar, events = client(
        delete=[http_error(410)],
        list=[{"items": [{"id": "a"}], "nextPageToken": "t"}, {"items": [{"id": "b"}]}],
    )
    assert calendar.delete_event("gone") is False
    assert [e["id"] for e in calendar.list_app_events()] == ["a", "b"]
    assert events.calls[1][1]["privateExtendedProperty"] == "created_by=onboarding-agent"


def test_from_settings_requires_shared_calendar(tmp_path):
    with pytest.raises(CalendarError, match="GCAL_CALENDAR_ID"):
        CalendarClient.from_settings(Settings())
    key = tmp_path / "sa.json"
    key.write_text('{"client_email": "bot@demo.iam.gserviceaccount.com"}', encoding="utf-8")
    with pytest.raises(CalendarError, match="공유받은 캘린더"):
        CalendarClient.from_settings(Settings(gcal_calendar_id="primary", google_service_account_json=str(key)))
