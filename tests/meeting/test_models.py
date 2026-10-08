from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from onboarding_agent.meeting.models import ActionItem, LLMExtraction, Meeting, new_id


def make_item(**overrides) -> ActionItem:
    data = {
        "item_id": new_id(),
        "meeting_id": "m1",
        "task": "보고서 초안 작성하기",
        "evidence_quote": "김민준 주무관이 초안을 맡기로 했다.",
    }
    data.update(overrides)
    return ActionItem(**data)


def test_new_item_defaults_are_safe():
    item = make_item()
    assert item.status == "draft"
    assert item.owner_status == "unconfirmed"
    assert item.due_status == "unconfirmed"
    assert item.calendar_event_id is None
    assert item.last_reminded_stage is None


def test_confirmed_owner_requires_name():
    with pytest.raises(ValidationError):
        make_item(owner_status="confirmed")
    assert make_item(owner_name="김민준", owner_status="confirmed").owner_status == "confirmed"


def test_confirmed_due_requires_date():
    with pytest.raises(ValidationError):
        make_item(due_status="confirmed")
    item = make_item(due_date="2026-10-16", due_time="15:00", due_status="confirmed")
    assert item.due_date == date(2026, 10, 16)
    assert item.due_time == time(15, 0)


def test_rejects_unknown_status_and_reminder_stage():
    with pytest.raises(ValidationError):
        make_item(status="sent")
    with pytest.raises(ValidationError):
        make_item(last_reminded_stage="D-2")


def test_llm_extraction_accepts_nulls_and_missing_optionals():
    extraction = LLMExtraction.model_validate(
        {
            "title_suggestion": None,
            "summary": ["요약"],
            "decisions": [{"text": "결정", "evidence_quote": "인용"}],
            "action_items": [
                {
                    "task": "자료 정리하기",
                    "owner_name": None,
                    "due_text": None,
                    "due_date_guess": None,
                    "due_time_guess": None,
                    "evidence_quote": "인용",
                }
            ],
        }
    )
    assert extraction.action_items[0].co_owners == []
    assert extraction.open_issues == []


def test_meeting_json_roundtrip_keeps_offset():
    meeting = Meeting(
        meeting_id="m1",
        title="주간 회의",
        meeting_date=date(2026, 10, 8),
        created_at=datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    restored = Meeting.model_validate_json(meeting.model_dump_json())
    assert restored == meeting
    assert restored.created_at.utcoffset().total_seconds() == 9 * 3600
