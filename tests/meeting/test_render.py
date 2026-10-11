import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from onboarding_agent.meeting.models import ActionItem, Decision, Meeting
from onboarding_agent.meeting.render import (
    APP_FOOTER,
    calendar_event,
    calendar_event_id,
    item_line,
    slack_followup,
    slack_summary,
)

MEETING = Meeting(
    meeting_id="m1",
    title="주간 회의",
    meeting_date=date(2026, 10, 8),
    summary=["자료를 보완한다."],
    decisions=[Decision(decision_id="d1", text="FAQ는 위키에 올린다.", evidence_quote="q")],
    created_at=datetime(2026, 10, 8, 10, tzinfo=ZoneInfo("Asia/Seoul")),
)


def item(**fields) -> ActionItem:
    data = {
        "item_id": "item-1",
        "meeting_id": "m1",
        "task": "자료 보완하기",
        "owner_name": "김민준",
        "owner_slack_id": "U123",
        "owner_status": "confirmed",
        "due_date": date(2026, 10, 16),
        "due_status": "confirmed",
        "evidence_quote": "자료는 김민준 주무관이 맡기로 함.",
        "status": "approved",
    }
    data.update(fields)
    return ActionItem(**data)


def test_event_id_is_deterministic_base32hex():
    first, second = calendar_event_id("item-1"), calendar_event_id("item-1")
    assert first == second
    assert re.fullmatch(r"[0-9a-v]{32}", first)
    assert calendar_event_id("item-2") != first


def test_all_day_event_ends_the_next_day_and_has_no_attendees():
    body = calendar_event(item(), MEETING)
    assert body["start"] == {"date": "2026-10-16"}
    assert body["end"] == {"date": "2026-10-17"}  # exclusive end date
    assert body["summary"] == "[액션] 자료 보완하기 (김민준)"
    assert "attendees" not in body
    assert body["extendedProperties"]["private"] == {
        "created_by": "onboarding-agent",
        "item_id": "item-1",
        "meeting_id": "m1",
    }
    assert "item_id: item-1" in body["description"]
    assert "주간 회의 (10/8(목))" in body["description"]


def test_timed_event_lasts_one_hour_in_seoul():
    body = calendar_event(item(due_time=time(15, 0)), MEETING)
    assert body["start"] == {"dateTime": "2026-10-16T15:00:00", "timeZone": "Asia/Seoul"}
    assert body["end"] == {"dateTime": "2026-10-16T16:00:00", "timeZone": "Asia/Seoul"}


def test_item_lines_mention_by_slack_id_or_flag_missing_slack():
    assert item_line(item()) == "• 자료 보완하기 — <@U123> · ~10/16(금)"
    unregistered = item(owner_name="이서연", owner_slack_id=None, due_time=time(14, 0), due_date=date(2026, 10, 12))
    assert item_line(unregistered) == "• 자료 보완하기 — 이서연(Slack 미등록) · ~10/12(월) 14:00"
    assert "&lt;script&gt;" in item_line(item(task="<script> 정리하기"))


def test_summary_message_has_text_fallback_and_blocks():
    message = slack_summary(MEETING, [item()])
    assert message.text.splitlines() == [
        "📋 회의 액션 아이템 | 주간 회의 (10/8(목))",
        "요약",
        "• 자료를 보완한다.",
        "결정사항",
        "• FAQ는 위키에 올린다.",
        "할 일",
        "• 자료 보완하기 — <@U123> · ~10/16(금)",
        APP_FOOTER,
    ]
    assert message.blocks[0]["type"] == "header"
    assert message.blocks[-1]["elements"][0]["text"] == APP_FOOTER
    assert any("<@U123>" in b.get("text", {}).get("text", "") for b in message.blocks)


def test_long_task_lists_split_into_several_sections():
    items = [item(item_id=f"i{n}", task="가" * 200) for n in range(40)]
    message = slack_summary(MEETING, items)
    sections = [b for b in message.blocks if b["type"] == "section"]
    assert all(len(b["text"]["text"]) <= 3000 for b in sections)
    assert len(sections) > 3


def test_followup_is_a_short_thread_reply():
    message = slack_followup(MEETING, [item()])
    assert message.text.startswith("➕ 추가로 승인된 할 일 | 주간 회의")
    assert APP_FOOTER in message.text
