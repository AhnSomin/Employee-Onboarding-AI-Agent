import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from onboarding_agent.meeting.models import ActionItem, Decision, Meeting
from onboarding_agent.meeting.render import (
    CLOSING,
    reminder_message,
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
    assert body["summary"] == "자료 보완하기 (김민준)"
    assert "attendees" not in body
    assert body["extendedProperties"]["private"] == {
        "created_by": "onboarding-agent",
        "item_id": "item-1",
        "meeting_id": "m1",
    }
    assert "item_id" not in body["description"]          # 식별은 extendedProperties가 맡는다
    assert "AI" not in body["description"] and "Agent" not in body["description"]
    assert "주간 회의 (10/8(목))에서 정해진 일입니다." in body["description"]
    assert "담당: 김민준" in body["description"]


def test_timed_event_lasts_one_hour_in_seoul():
    body = calendar_event(item(due_time=time(15, 0)), MEETING)
    assert body["start"] == {"dateTime": "2026-10-16T15:00:00", "timeZone": "Asia/Seoul"}
    assert body["end"] == {"dateTime": "2026-10-16T16:00:00", "timeZone": "Asia/Seoul"}


def test_item_lines_mention_by_slack_id_or_name():
    assert item_line(item()) == "• 자료 보완하기: <@U123> 님, 10/16(금)까지"
    unregistered = item(owner_name="이서연", owner_slack_id=None, due_time=time(14, 0), due_date=date(2026, 10, 12))
    assert item_line(unregistered) == "• 자료 보완하기: 이서연 님, 10/12(월) 14:00까지"
    assert "Slack 미등록" not in item_line(unregistered)          # 명단에 없어도 사람 글처럼 이름만 쓴다
    assert "&lt;script&gt;" in item_line(item(task="<script> 정리하기"))


def test_summary_reads_like_a_person_wrote_it():
    message = slack_summary(MEETING, [item()])
    assert message.text.splitlines() == [
        "안녕하세요, 10/8(목) 주간 회의 내용 정리해서 공유드립니다.",
        "",
        "*논의한 내용*",
        "• 자료를 보완한다.",
        "",
        "*정해진 내용*",
        "• FAQ는 위키에 올린다.",
        "",
        "*앞으로 할 일*",
        "• 자료 보완하기: <@U123> 님, 10/16(금)까지",
        "",
        CLOSING,
    ]
    assert message.blocks == []                                  # 블록·머리글 없이 일반 메시지로 보낸다
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", message.text)   # 이모지 없음
    assert "Agent" not in message.text and "AI" not in message.text


def test_summary_skips_empty_sections():
    bare = MEETING.model_copy(update={"summary": [], "decisions": []})
    text = slack_summary(bare, [item()]).text
    assert "논의한 내용" not in text and "정해진 내용" not in text and "(요약 없음)" not in text
    assert "*앞으로 할 일*" in text


def test_followup_is_a_short_polite_thread_reply():
    message = slack_followup(MEETING, [item()])
    assert message.text.splitlines() == [
        "회의 이후에 확정된 할 일이 있어서 추가로 공유드립니다.",
        "• 자료 보완하기: <@U123> 님, 10/16(금)까지",
        "확인 부탁드립니다.",
    ]


def test_reminders_are_short_checkins_per_stage():
    one = item(due_time=time(15, 0))
    d1, day, late = (reminder_message(one, s).text for s in ("D-1", "D-day", "overdue"))
    assert "마감이 내일" in d1 and "오늘까지" in day and "기한이 지났" in late
    for text in (d1, day, late):
        assert text.startswith("<@U123> 님,") and "10/16(금) 15:00" in text
        assert "Agent" not in text and "⏰" not in text and "액션 현황" not in text
