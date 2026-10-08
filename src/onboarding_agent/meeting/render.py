"""Calendar and Slack payloads for approved action items (spec 8.5).

The same output is shown as the preview before approval and sent on
execution. Every payload carries the app marker (CREATED_BY) so demo data
can be found and removed later by scripts/reset_demo.py.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .dates import format_due
from .models import CREATED_BY, ActionItem, Meeting

APP_FOOTER = "회의록 승인 후 온보딩 Agent가 보낸 메시지입니다."
CALENDAR_FOOTER = "이 일정은 신입사원 온보딩 AI Agent가 회의록 승인 후 만들었습니다."
EVENT_DURATION = timedelta(hours=1)
_SECTION_LIMIT = 2900  # Slack section text limit is 3000 characters


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
        "summary": f"[액션] {item.task} ({owner_label(item)})",
        "description": "\n".join(
            [
                f"회의: {meeting.title} ({format_due(meeting.meeting_date)})",
                f"담당: {owner_label(item)}",
                f"근거: “{item.evidence_quote}”",
                f"item_id: {item.item_id}",
                CALENDAR_FOOTER,
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


def _due_label(item: ActionItem) -> str:
    return "~" + format_due(item.due_date, item.due_time) if item.due_date else "기한 미정"


def item_line(item: ActionItem) -> str:
    """'• 할 일 — <@U…> · ~10/16(금)' or '• 할 일 — 이서연(Slack 미등록) · ~10/12(월) 14:00'."""
    if item.owner_slack_id:
        who = f"<@{item.owner_slack_id}>"
    elif item.owner_name:
        who = f"{_escape(item.owner_name)}(Slack 미등록)"
    else:
        who = "담당 미정"
    return f"• {_escape(item.task)} — {who} · {_due_label(item)}"


def _section(title: str, lines: list[str]) -> list[dict[str, Any]]:
    """One or more section blocks, split to stay under Slack's text limit."""
    blocks: list[dict[str, Any]] = []
    chunk = f"*{title}*"
    for line in lines:
        if len(chunk) + len(line) + 1 > _SECTION_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    return blocks


def slack_summary(meeting: Meeting, items: list[ActionItem]) -> SlackMessage:
    """One message per meeting: summary, decisions and the approved tasks."""
    header = f"📋 회의 액션 아이템 | {meeting.title} ({format_due(meeting.meeting_date)})"
    summary = [f"• {_escape(s)}" for s in meeting.summary] or ["• (요약 없음)"]
    decisions = [f"• {_escape(d.text)}" for d in meeting.decisions] or ["• (결정사항 없음)"]
    tasks = [item_line(i) for i in items]
    text = "\n".join([header, "요약", *summary, "결정사항", *decisions, "할 일", *tasks, APP_FOOTER])
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150], "emoji": True}},
        *_section("요약", summary),
        *_section("결정사항", decisions),
        *_section("할 일", tasks),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": APP_FOOTER}]},
    ]
    return SlackMessage(text=text, blocks=blocks)


def slack_followup(meeting: Meeting, items: list[ActionItem]) -> SlackMessage:
    """Thread reply for items approved after the summary was sent."""
    header = f"➕ 추가로 승인된 할 일 | {meeting.title}"
    tasks = [item_line(i) for i in items]
    text = "\n".join([header, *tasks, APP_FOOTER])
    blocks = [
        *_section(header, tasks),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": APP_FOOTER}]},
    ]
    return SlackMessage(text=text, blocks=blocks)
