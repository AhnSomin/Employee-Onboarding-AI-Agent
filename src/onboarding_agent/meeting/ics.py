"""승인된 액션 아이템을 캘린더 파일(.ics)로 만든다. Google Calendar·Outlook·Apple 캘린더에서 열어 가져올 수 있다.

일정 내용(제목·설명·종일/1시간)은 Google Calendar 연동과 같은 `render.calendar_event`를 그대로 쓴다.
시각이 있는 일정은 UTC로 바꿔 적어 시간대 정의 없이도 어느 캘린더에서나 같은 시각으로 열린다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .models import ActionItem, Meeting
from .render import calendar_event, calendar_event_id


def _esc(text: str) -> str:
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\n").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545: 한 줄은 75바이트 이하. 넘으면 다음 줄을 공백으로 시작해 이어 쓴다 (UTF-8 글자 경계를 지킨다)."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 74:
            out.append(cur)
            cur, size = " " + ch, 1 + n
        else:
            cur, size = cur + ch, size + n
    out.append(cur)
    return out


def _utc(local_iso: str, tz: str) -> str:
    dt = datetime.fromisoformat(local_iso).replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(meeting: Meeting, items: list[ActionItem], tz: str = "Asia/Seoul", now: datetime | None = None) -> tuple[str, int]:
    """(.ics 텍스트, 일정 개수). 기한이 있는 항목만 일정이 된다."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//onboarding-agent//KR", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    count = 0
    for item in items:
        if item.due_date is None:
            continue
        body = calendar_event(item, meeting, tz)
        lines += ["BEGIN:VEVENT", f"UID:{calendar_event_id(item.item_id)}@onboarding-agent", f"DTSTAMP:{stamp}",
                  f"SUMMARY:{_esc(body['summary'])}", f"DESCRIPTION:{_esc(body['description'])}"]
        if "date" in body["start"]:
            lines += [f"DTSTART;VALUE=DATE:{date.fromisoformat(body['start']['date']):%Y%m%d}",
                      f"DTEND;VALUE=DATE:{date.fromisoformat(body['end']['date']):%Y%m%d}"]
        else:
            lines += [f"DTSTART:{_utc(body['start']['dateTime'], tz)}", f"DTEND:{_utc(body['end']['dateTime'], tz)}"]
        lines.append("END:VEVENT")
        count += 1
    lines.append("END:VCALENDAR")
    folded = [piece for line in lines for piece in _fold(line)]
    return "\r\n".join(folded) + "\r\n", count
