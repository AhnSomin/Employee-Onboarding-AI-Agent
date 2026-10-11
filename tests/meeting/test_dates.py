from datetime import date, time

import pytest

from onboarding_agent.meeting.dates import (
    find_absolute_date,
    find_due_text,
    format_due,
    resolve_due,
)

MEETING = date(2026, 10, 8)  # Thursday


@pytest.mark.parametrize(
    ("text", "expected_date", "expected_time"),
    [
        # Spec 8.3 table
        ("내일", date(2026, 10, 9), None),
        ("모레", date(2026, 10, 10), None),
        ("이번 주 금요일", date(2026, 10, 9), None),
        ("다음 주 금요일", date(2026, 10, 16), None),
        ("월요일까지", date(2026, 10, 12), None),
        ("10월 20일", date(2026, 10, 20), None),
        ("10/20 오후 3시", date(2026, 10, 20), time(15, 0)),
        ("3일 이내", date(2026, 10, 11), None),
        # Other supported expressions
        ("오늘 중", date(2026, 10, 8), None),
        ("금일", date(2026, 10, 8), None),
        ("명일 오전 10시", date(2026, 10, 9), time(10, 0)),
        ("내일모레", date(2026, 10, 10), None),
        ("다다음 주 월요일", date(2026, 10, 19), None),
        ("차주 화요일", date(2026, 10, 13), None),
        ("목요일까지", date(2026, 10, 15), None),  # nearest future, never the meeting day
        ("10.20", date(2026, 10, 20), None),
        ("2026-10-20", date(2026, 10, 20), None),
        ("2026.10.20(화) 14:30", date(2026, 10, 20), time(14, 30)),
        ("2026년 11월 2일", date(2026, 11, 2), None),
        ("20일까지", date(2026, 10, 20), None),
        ("이번 달 20일까지", date(2026, 10, 20), None),
        ("다음 달 5일", date(2026, 11, 5), None),
        ("이번 달 말일", date(2026, 10, 31), None),
        ("5일 후", date(2026, 10, 13), None),
        ("일주일 이내", date(2026, 10, 15), None),
        ("2주 안에", date(2026, 10, 22), None),
        ("10/12(월) 오후 3시까지", date(2026, 10, 12), time(15, 0)),
        ("다음 주 월요일 오전 10시 30분", date(2026, 10, 12), time(10, 30)),
        ("10월 14일(수) 오후 2시 반", date(2026, 10, 14), time(14, 30)),
        ("내일 낮 1시", date(2026, 10, 9), time(13, 0)),
        ("내일 오전 12시", date(2026, 10, 9), time(0, 0)),
        ("내일 15시", date(2026, 10, 9), time(15, 0)),
    ],
)
def test_specific_expressions_are_confirmed(text, expected_date, expected_time):
    result = resolve_due(text, MEETING)
    assert result.due_date == expected_date
    assert result.due_time == expected_time
    assert result.confirmed, result.notes


@pytest.mark.parametrize(
    "text",
    ["이번 주 중", "월말", "다음 달 초", "조만간", "가능한 빨리", "다음 회의 전", "이번주 중으로", "추후"],
)
def test_vague_expressions_stay_unconfirmed(text):
    result = resolve_due(text, MEETING)
    assert result.due_date is None
    assert not result.confirmed
    assert result.notes == ("일 단위로 특정할 수 없는 기한입니다.",)


def test_year_less_past_date_rolls_to_next_year_unconfirmed():
    result = resolve_due("10월 5일까지", MEETING)
    assert result.due_date == date(2027, 10, 5)
    assert not result.confirmed
    assert "2027년" in result.notes[0]


def test_past_dates_are_unconfirmed():
    for text in ("2026-10-01", "이번 주 화요일", "5일까지"):
        result = resolve_due(text, MEETING)
        assert not result.confirmed, text
        assert "회의 날짜보다 앞선 기한입니다." in result.notes


def test_weekday_annotation_must_match():
    result = resolve_due("10/16(목)", MEETING)  # 10/16 is a Friday
    assert result.due_date == date(2026, 10, 16)
    assert not result.confirmed
    assert "요일(목)" in result.notes[0]


def test_bare_hour_without_am_pm_leaves_time_empty():
    result = resolve_due("내일 3시", MEETING)
    assert result.due_date == date(2026, 10, 9)
    assert result.due_time is None
    assert not result.confirmed


def test_hours_duration_is_not_a_clock_time():
    result = resolve_due("내일 3시간 안에", MEETING)
    assert result.due_date == date(2026, 10, 9)
    assert result.due_time is None
    assert result.confirmed


def test_two_dates_are_ambiguous():
    result = resolve_due("10월 12일 또는 10월 13일", MEETING)
    assert result.due_date is None
    assert not result.confirmed


def test_unreadable_and_empty_expressions():
    assert resolve_due(None, MEETING).notes == ("기한 표현이 없습니다.",)
    assert resolve_due("담당자 확인 후", MEETING).notes == ("기한 표현을 해석하지 못했습니다.",)
    assert not resolve_due("매주 목요일", MEETING).confirmed
    assert resolve_due("2월 30일", MEETING).due_date is None


def test_month_boundaries():
    december = date(2026, 12, 30)
    assert resolve_due("다음 달 말일", december).due_date == date(2027, 1, 31)
    assert resolve_due("모레", december).due_date == date(2027, 1, 1)
    assert resolve_due("1월 5일", december).due_date == date(2027, 1, 5)


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("오리엔테이션 자료 추가: 김민준 주무관, 10월 14일(수)까지", "10월 14일(수)까지"),
        ("정하은 주무관이 3일 이내에 취합하기로 함.", "3일 이내"),
        ("최 주무관이 다음 주 월요일 오전 10시까지 올리기로 함.", "다음 주 월요일 오전 10시까지"),
        ("보안 서약서 회수 현황은 월말까지 정리해야 함.", "월말까지"),
        ("교육 일정표 수정본 배포는 이번 주 중으로 진행.", "이번 주 중"),
        ("비품 양식 제작: 박지훈 주무관, 10/12(월) 오후 3시까지", "10/12(월) 오후 3시까지"),
        ("멘토링 장소를 정해야 함.", None),
    ],
)
def test_find_due_text(sentence, expected):
    assert find_due_text(sentence) == expected


def test_find_absolute_date_and_format():
    assert find_absolute_date("일시: 2026.10.08(목) 14:00", 2025) == date(2026, 10, 8)
    assert find_absolute_date("일시: 10월 8일(목) 16:00", 2026) == date(2026, 10, 8)
    assert find_absolute_date("참석자: 김민준", 2026) is None
    assert format_due(date(2026, 10, 16), time(15, 0)) == "10/16(금) 15:00"
