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
    assert item_line(item()) == "• <@U123> 님 — 자료 보완하기"
    unregistered = item(owner_name="이서연", owner_slack_id=None, due_time=time(14, 0), due_date=date(2026, 10, 12))
    assert item_line(unregistered) == "• 14:00 · 이서연 님 — 자료 보완하기"
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
        "*일정*",
        "",
        "*10/16(금) 마감*",
        "• <@U123> 님 — 자료 보완하기",
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
    assert "*일정*" in text


def test_followup_is_a_short_polite_thread_reply():
    message = slack_followup(MEETING, [item()])
    assert message.text.splitlines() == [
        "회의 이후에 확정된 할 일이 있어서 추가로 공유드립니다.",
        "",
        "*10/16(금) 마감*",
        "• <@U123> 님 — 자료 보완하기",
        "",
        "확인 부탁드립니다.",
    ]


def test_reminders_are_short_checkins_per_stage():
    one = item(due_time=time(15, 0))
    d1, day, late = (reminder_message(one, s).text for s in ("D-1", "D-day", "overdue"))
    assert "마감이 내일" in d1 and "오늘까지" in day and "기한이 지났" in late
    for text in (d1, day, late):
        assert text.startswith("<@U123> 님,") and "10/16(금) 15:00" in text
        assert "Agent" not in text and "⏰" not in text and "액션 현황" not in text


def test_summary_lines_repeated_in_decisions_appear_once():
    both = MEETING.model_copy(update={"summary": ["FAQ는 위키에 올린다.", "자료를 보완한다."]})
    text = slack_summary(both, [item()]).text
    assert text.count("FAQ는 위키에 올린다.") == 1 and "자료를 보완한다." in text
    only_decisions = MEETING.model_copy(update={"summary": ["FAQ는 위키에 올린다."]})
    assert "논의한 내용" not in slack_summary(only_decisions, [item()]).text


def test_honorific_adds_nim_to_bare_names_only():
    from onboarding_agent.meeting.render import honorific

    names = ["오태곤", "정준재"]
    assert honorific("오태곤이 월요일까지 만들기로 했습니다.", names) == "오태곤 님이 월요일까지 만들기로 했습니다."
    assert honorific("정준재 주무관과 오태곤님이 협의", names) == "정준재 주무관과 오태곤님이 협의"
    assert honorific("한오태곤이", names) == "한오태곤이"          # 다른 이름의 일부는 건드리지 않는다
    assert honorific("인사팀이 확인", names) == "인사팀이 확인"
    assert honorific("정준재가 작성, 정준재는 검토, 정준재를 지정", names) == "정준재 님이 작성, 정준재 님은 검토, 정준재 님을 지정"


def test_schedule_is_sorted_by_due_with_undated_last():
    from onboarding_agent.meeting.render import by_due

    late = item(item_id="a", task="나중", due_date=date(2026, 10, 20), due_time=None)
    early_pm = item(item_id="b", task="이른 오후", due_date=date(2026, 10, 14), due_time=time(15, 0))
    early_am = item(item_id="c", task="이른 오전", due_date=date(2026, 10, 14), due_time=time(9, 0))
    undated = item(item_id="d", task="미정", due_date=None, due_time=None, due_status="unconfirmed")
    assert [i.task for i in by_due([undated, late, early_pm, early_am])] == ["이른 오전", "이른 오후", "나중", "미정"]
    from onboarding_agent.meeting.render import schedule_lines

    assert schedule_lines([undated, early_pm, early_am, late]) == [
        "*10/14(수) 마감*",
        "• 09:00 · <@U123> 님 — 이른 오전",
        "• 15:00 · <@U123> 님 — 이른 오후",
        "",
        "*10/20(화) 마감*",
        "• <@U123> 님 — 나중",
        "",
        "*기한 미정*",
        "• <@U123> 님 — 미정",
    ]
