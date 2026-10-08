"""StateStore contract. Every backend must pass the same tests (spec 9.2)."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from onboarding_agent.meeting.models import ActionItem, Meeting, new_id
from onboarding_agent.store.base import NotFoundError
from onboarding_agent.store.sqlite_store import SqliteStore

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(params=["sqlite", "sheets"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SqliteStore(tmp_path / "state.db")
    pytest.skip("SheetsStore not implemented yet; it will run only when credentials are set")


def make_meeting(meeting_id: str = "m1", title: str = "주간 회의") -> Meeting:
    return Meeting(
        meeting_id=meeting_id,
        title=title,
        meeting_date=date(2026, 10, 8),
        summary=["요약"],
        created_at=datetime(2026, 10, 8, 10, 0, tzinfo=KST),
    )


def make_item(meeting_id: str = "m1", **overrides) -> ActionItem:
    data = {
        "item_id": new_id(),
        "meeting_id": meeting_id,
        "task": "자료 정리하기",
        "evidence_quote": "자료는 다음 주까지 정리하기로 했다.",
    }
    data.update(overrides)
    return ActionItem(**data)


def test_meeting_roundtrip_and_missing(store):
    meeting = make_meeting()
    store.save_meeting(meeting)
    assert store.get_meeting("m1") == meeting
    assert store.get_meeting("absent") is None


def test_saving_meeting_twice_does_not_duplicate(store):
    store.save_meeting(make_meeting(title="v1"))
    store.save_meeting(make_meeting(title="v2"))
    assert [m.title for m in store.list_meetings()] == ["v2"]


def test_meetings_listed_in_insertion_order(store):
    for meeting_id in ["b", "a", "c"]:
        store.save_meeting(make_meeting(meeting_id=meeting_id))
    assert [m.meeting_id for m in store.list_meetings()] == ["b", "a", "c"]


def test_update_meeting_persists(store):
    store.save_meeting(make_meeting())
    updated = store.update_meeting("m1", slack_ts="1700000000.000100")
    assert updated.slack_ts == "1700000000.000100"
    assert store.get_meeting("m1") == updated


def test_update_meeting_rejects_bad_input(store):
    store.save_meeting(make_meeting())
    with pytest.raises(NotFoundError):
        store.update_meeting("absent", title="x")
    with pytest.raises(ValueError):
        store.update_meeting("m1", no_such_field=1)


def test_upsert_items_updates_in_place(store):
    first, second = make_item(task="첫째"), make_item(task="둘째")
    store.upsert_items([first, second])
    store.upsert_items([first.model_copy(update={"task": "첫째 수정"})])
    assert [i.task for i in store.list_items()] == ["첫째 수정", "둘째"]


def test_list_items_filters_by_status_and_meeting(store):
    draft = make_item("m1")
    approved = make_item("m1", status="approved")
    other_meeting = make_item("m2", status="approved")
    store.upsert_items([draft, approved, other_meeting])

    def ids(items):
        return [i.item_id for i in items]

    assert ids(store.list_items(status="approved")) == [approved.item_id, other_meeting.item_id]
    assert ids(store.list_items(meeting_id="m1")) == [draft.item_id, approved.item_id]
    assert ids(store.list_items(status="approved", meeting_id="m2")) == [other_meeting.item_id]


def test_update_item_validates_and_persists(store):
    item = make_item()
    store.upsert_items([item])
    approved_at = datetime(2026, 10, 8, 11, 30, tzinfo=KST)
    updated = store.update_item(
        item.item_id,
        status="approved",
        approved_at=approved_at,
        owner_name="김민준",
        owner_status="confirmed",
        due_date="2026-10-16",
        due_time=time(15, 0),
        due_status="confirmed",
    )
    assert updated.due_date == date(2026, 10, 16)
    stored = store.get_item(item.item_id)
    assert stored == updated
    assert stored.approved_at == approved_at
    assert store.list_items(status="approved") == [updated]


def test_update_item_rejects_bad_input_and_keeps_row(store):
    item = make_item()
    store.upsert_items([item])
    with pytest.raises(NotFoundError):
        store.update_item("absent", status="done")
    with pytest.raises(ValidationError):
        store.update_item(item.item_id, status="sent")
    with pytest.raises(ValidationError):
        store.update_item(item.item_id, owner_status="confirmed")  # no owner name
    with pytest.raises(ValueError):
        store.update_item(item.item_id, no_such_field=1)
    assert store.get_item(item.item_id) == item


def test_sqlite_data_survives_reopen(tmp_path):
    path = tmp_path / "state.db"
    SqliteStore(path).save_meeting(make_meeting())
    assert SqliteStore(path).get_meeting("m1") is not None
