"""Calendar and Slack payloads for approved action items (spec 8.5).

The same output is shown as the preview before approval and sent on
execution. Every payload carries the app marker (CREATED_BY) so demo data
can be found and removed later by scripts/reset_demo.py.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from collections.abc import Iterable
from typing import Any

from .dates import format_due
from .models import CREATED_BY, ActionItem, Meeting, ReminderStage
from .roster import TITLES

EVENT_DURATION = timedelta(hours=1)
STAGE_LABELS: dict[str, str] = {"D-1": "D-1", "D-day": "D-day", "overdue": "기한 지남"}  # 화면·CLI 표시용 (Slack 문구에는 쓰지 않음)
CLOSING = "빠진 내용이나 고칠 부분 있으면 편하게 말씀해 주세요. 감사합니다."


@dataclass(frozen=True)
class SlackMessage:
    text: str  # plain-text fallback (notifications, clients without blocks)
    blocks: list[dict[str, Any]] = field(default_factory=list)


def calendar_event_id(item_id: str) -> str:
    """Deterministic event id from the item id: base32hex (0-9, a-v), 32 characters.

    Re-running an approval therefore hits a 409 conflict instead of creating
    a second event.
    """
    digest = hashlib.sha1(item_id.encode("utf-8")).digest()
    return base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")


def owner_label(item: ActionItem) -> str:
    return item.owner_name or "담당 미정"


def calendar_event(item: ActionItem, meeting: Meeting, timezone: str = "Asia/Seoul") -> dict[str, Any]:
    """Event body for the Calendar API. All-day when there is no time; no attendees."""
    if item.due_date is None:
        raise ValueError("an approved item needs a due date")
    body: dict[str, Any] = {
        "id": calendar_event_id(item.item_id),
        "summary": f"{item.task} ({owner_label(item)})",
        "description": "\n".join(
            [
                f"{meeting.title} ({format_due(meeting.meeting_date)})에서 정해진 일입니다.",
                f"담당: {owner_label(item)}",
                f"회의록 메모: “{item.evidence_quote}”",
            ]
        ),
        "extendedProperties": {
            "private": {
                "created_by": CREATED_BY,
                "item_id": item.item_id,
                "meeting_id": meeting.meeting_id,
            }
        },
    }
    if item.due_time is None:
        body["start"] = {"date": item.due_date.isoformat()}
        # The end date of an all-day event is exclusive.
        body["end"] = {"date": (item.due_date + timedelta(days=1)).isoformat()}
    else:
        start = datetime.combine(item.due_date, item.due_time)
        body["start"] = {"dateTime": start.isoformat(timespec="seconds"), "timeZone": timezone}
        body["end"] = {
            "dateTime": (start + EVENT_DURATION).isoformat(timespec="seconds"),
            "timeZone": timezone,
        }
    return body


def _escape(text: str) -> str:
    """Slack mrkdwn escaping for user content (mentions are added after this)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _person(item: ActionItem) -> str:
    """'<@U123> 님' (Slack 멘션) 또는 명단에 없으면 '김민준 님'."""
    if item.owner_slack_id:
        return f"<@{item.owner_slack_id}> 님"
    if item.owner_name:
        return f"{_escape(item.owner_name)} 님"
    return "담당자"


def item_line(item: ActionItem) -> str:
    """'• 15:00 · 김민준 님 — 자료 보완하기' — 날짜는 묶음 제목에 있으니 시각이 있을 때만 앞에 붙인다."""
    at = f"{item.due_time:%H:%M} · " if item.due_time else ""
    return f"• {at}{_person(item)} — {_escape(item.task)}"


def by_due(items: list[ActionItem]) -> list[ActionItem]:
    """기한이 빠른 순. 기한이 없는 항목은 맨 뒤 (같으면 입력 순서 유지)."""
    return sorted(items, key=lambda i: (i.due_date is None, i.due_date or date.max, i.due_time or time.max))


def schedule_lines(items: list[ActionItem]) -> list[str]:
    """달력 일정표처럼 마감일별로 묶는다. 날짜만 굵게, 그 아래에 시각·담당자·할 일."""
    lines: list[str] = []
    last: object = object()
    for item in by_due(items):
        if item.due_date != last:
            if lines:
                lines.append("")
            lines.append(f"*{format_due(item.due_date)} 마감*" if item.due_date else "*기한 미정*")
            last = item.due_date
        lines.append(item_line(item))
    return lines


_HONORIFIC_AFTER = ("님", "씨", *TITLES)
_JOSA_AFTER_NIM = {"가": "이", "는": "은", "를": "을", "와": "과"}   # '님'은 받침이 있어서 조사가 바뀐다


def honorific(text: str, names: Iterable[str]) -> str:
    """문장 속 이름 뒤에 ' 님'을 붙인다 ('오태곤이' → '오태곤 님이', '정준재가' → '정준재 님이'). 이미 님·씨·직함이 붙은 이름은 그대로 둔다."""
    for name in sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True):
        after = "|".join(re.escape(t) for t in _HONORIFIC_AFTER)
        text = re.sub(
            rf"(?<![가-힣]){re.escape(name)}(?!\s?(?:{after}))([가는를와])?",
            lambda m, n=name: f"{n} 님{_JOSA_AFTER_NIM.get(m.group(1) or '', m.group(1) or '')}",
            text,
        )
    return text


def slack_summary(meeting: Meeting, items: list[ActionItem], names: Iterable[str] = ()) -> SlackMessage:
    """회의 한 건당 메시지 하나. 사람이 정리해 올린 것처럼 인사로 시작해 부탁으로 맺는다 (블록·이모지·꼬리말 없음)."""
    lines = [f"안녕하세요, {format_due(meeting.meeting_date)} {_escape(meeting.title)} 내용 정리해서 공유드립니다.", ""]
    people = {*names, *(i.owner_name for i in items if i.owner_name), *(c for i in items for c in i.co_owners)}
    decided = {" ".join(d.text.split()) for d in meeting.decisions}
    discussed = [t for t in meeting.summary if " ".join(t.split()) not in decided]   # 결정사항과 같은 문장은 한 번만
    if discussed:
        lines += ["*논의한 내용*", *(f"• {honorific(_escape(t), people)}" for t in discussed), ""]
    if meeting.decisions:
        lines += ["*정해진 내용*", *(f"• {honorific(_escape(d.text), people)}" for d in meeting.decisions), ""]
    lines += ["*일정*", "", *schedule_lines(items), "", CLOSING]
    return SlackMessage(text="\n".join(lines))


def slack_followup(meeting: Meeting, items: list[ActionItem]) -> SlackMessage:
    """요약을 올린 뒤 새로 확정된 할 일 (스레드 답글)."""
    lines = ["회의 이후에 확정된 할 일이 있어서 추가로 공유드립니다.", "", *schedule_lines(items), "", "확인 부탁드립니다."]
    return SlackMessage(text="\n".join(lines))


_REMINDERS: dict[str, str] = {
    "D-1": "{who}, '{task}' 마감이 내일이에요 ({due}). 진행은 잘 되고 계실까요? 도움이 필요하면 말씀해 주세요.",
    "D-day": "{who}, '{task}' 오늘까지예요 ({due}). 마무리되면 한 줄만 남겨 주세요.",
    "overdue": "{who}, '{task}' 기한이 지났어요 ({due}). 진행 상황 알려 주실 수 있을까요? 일정 조정이 필요하면 말씀해 주세요.",
}


def reminder_message(item: ActionItem, stage: ReminderStage) -> SlackMessage:
    """기한 전후 재알림. 담당자를 부르는 짧은 안부 한 줄 (스레드 답글)."""
    due = format_due(item.due_date, item.due_time) if item.due_date else "기한 미정"
    return SlackMessage(text=_REMINDERS[stage].format(who=_person(item), task=_escape(item.task), due=due))
