from datetime import date

import pytest

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.loader import (
    MeetingInputError,
    decode_bytes,
    detect_meeting_date,
    load_meeting,
    suggest_title,
)

TODAY = date(2026, 10, 9)
SAMPLES = REPO_ROOT / "data" / "samples"
KOREAN = "회의명: 주간 회의\r\n일시: 2026.10.08(목)\r\n- 자료는 김민준 주무관이 맡기로 함.\r\n"


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        (KOREAN.encode("utf-8"), "utf-8"),
        (KOREAN.encode("utf-8-sig"), "utf-8-sig"),
        (KOREAN.encode("cp949"), "cp949"),
    ],
)
def test_decodes_utf8_bom_and_cp949(data, encoding):
    loaded = load_meeting(data=data, filename="minutes.txt", today=TODAY)
    assert loaded.encoding == encoding
    assert loaded.text.startswith("회의명: 주간 회의\n")  # BOM stripped, CRLF normalized
    assert "\r" not in loaded.text
    assert loaded.meeting_date == date(2026, 10, 8)
    assert loaded.meeting_date_detected


def test_undecodable_bytes_raise_user_message():
    with pytest.raises(MeetingInputError):
        decode_bytes(b"\xff\xfe\x00\xd8\x00")


@pytest.mark.parametrize(
    ("name", "title", "meeting_date"),
    [
        ("01_structured_minutes.txt", "10월 2주차 신규 임용자 온보딩 점검 회의", date(2026, 10, 8)),
        ("02_transcript.txt", "신규 임용자 멘토링 운영 회의", date(2026, 10, 8)),
        ("03_edge_cases.txt", "온보딩 TF 수시 회의 메모", date(2026, 10, 8)),
    ],
)
def test_samples_title_and_date(name, title, meeting_date):
    loaded = load_meeting(data=(SAMPLES / name).read_bytes(), filename=name, today=TODAY)
    assert loaded.title_suggestion == title
    assert loaded.meeting_date == meeting_date
    assert loaded.meeting_date_detected


def test_year_less_date_uses_current_year_and_missing_date_uses_today():
    assert detect_meeting_date("일시: 10월 8일(목) 16:00", TODAY) == date(2026, 10, 8)
    loaded = load_meeting(text="- 자료 정리하기", today=TODAY)
    assert loaded.meeting_date == TODAY
    assert not loaded.meeting_date_detected


def test_labeled_date_wins_over_deadlines_in_body():
    text = "주간 회의\n- 보고서는 10월 20일까지 제출\n일시: 2026-10-07(수)\n"
    assert detect_meeting_date(text, TODAY) == date(2026, 10, 7)


def test_title_from_bracket_or_filename():
    assert suggest_title("[시설 점검 회의] 2026.03.02(월)\n- 내용") == "시설 점검 회의"
    assert suggest_title("일시: 2026.03.02\n", filename="주간_업무_회의.txt") == "주간 업무 회의"
    assert suggest_title("", filename=None) == "제목 없는 회의"


def test_rejects_empty_too_long_and_unsupported_files():
    with pytest.raises(MeetingInputError):
        load_meeting(text="  \n ", today=TODAY)
    with pytest.raises(MeetingInputError, match="너무 깁니다"):
        load_meeting(text="가" * 101, max_chars=100, today=TODAY)
    with pytest.raises(MeetingInputError):
        load_meeting(data=b"x", filename="minutes.docx", today=TODAY)
    with pytest.raises(MeetingInputError):
        load_meeting(today=TODAY)
