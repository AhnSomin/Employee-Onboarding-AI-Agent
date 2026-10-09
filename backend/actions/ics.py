"""승인/상신된 연가를 캘린더(.ics) 파일로 만든다."""
from datetime import date, timedelta

TIMES = {"오전반차": ("090000", "130000"), "오후반차": ("140000", "180000")}


def leave_ics(payload: dict, req_id: int) -> str:
    d = date.fromisoformat(payload["일자"])
    t = payload["시간구분"]
    if t in TIMES:
        s, e = TIMES[t]
        when = f"DTSTART;TZID=Asia/Seoul:{d:%Y%m%d}T{s}\nDTEND;TZID=Asia/Seoul:{d:%Y%m%d}T{e}"
    else:
        when = f"DTSTART;VALUE=DATE:{d:%Y%m%d}\nDTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//onboarding-agent//KR\nBEGIN:VEVENT\n"
            f"UID:leave-{req_id}@onboarding-agent\nDTSTAMP:{date.today():%Y%m%d}T000000Z\n"
            f"SUMMARY:연가 ({t})\nDESCRIPTION:{payload['사유']}\n{when}\nEND:VEVENT\nEND:VCALENDAR\n").replace("\n", "\r\n")
