"""승인된 액션 아이템 → Slack 알림 + 캘린더(.ics) 생성."""
import json
import os
from datetime import date, datetime, timedelta, timezone

import requests

from backend import config


def load_team() -> list[dict]:
    return json.loads((config.DATA_DIR / "team.json").read_text())


def _mention(owner: str | None, team: list[dict]) -> str:
    if not owner:
        return "*(담당자 미정)*"
    for m in team:
        if m["name"] == owner or m["name"] in owner or owner in m["name"]:
            return f'<@{m["slack_id"]}>' if m.get("slack_id") else f"*{owner}*"
    return f"*{owner}*"


def slack_text(title: str, items: list[dict]) -> str:
    team = load_team()
    lines = [f"📋 *{title}* — 액션 아이템 {len(items)}건"]
    for it in items:
        due = f" · 기한 {it['due']}" if it.get("due") else " · 기한 미정"
        lines.append(f"• {it['task']} — {_mention(it.get('owner'), team)}{due}")
    return "\n".join(lines)


def send_slack(text: str) -> dict:
    url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return {"sent": False, "reason": "SLACK_WEBHOOK_URL이 설정되지 않아 발송하지 않고 미리보기만 만들었어요.", "preview": text}
    try:
        r = requests.post(url, json={"text": text}, timeout=10)
        r.raise_for_status()
        return {"sent": True, "preview": text}
    except Exception as e:
        return {"sent": False, "reason": f"Slack 발송 실패: {type(e).__name__}", "preview": text}


def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def calendar_ics(title: str, items: list[dict]) -> str:
    """기한이 있는 항목만 종일 일정으로. Google Calendar·Outlook에서 파일을 열어 가져올 수 있다."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ev = []
    for i, it in enumerate(x for x in items if x.get("due")):
        d = date.fromisoformat(it["due"])
        desc = f'회의: {title}\n담당: {it.get("owner") or "미정"}'
        ev.append("BEGIN:VEVENT\n" f"UID:{stamp}-{i}@onboarding-agent\nDTSTAMP:{stamp}\n"
                  f"SUMMARY:{_esc(it['task'])}\nDESCRIPTION:{_esc(desc)}\n"
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}\nDTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}\nEND:VEVENT")
    body = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//onboarding-agent//KR\n" + "\n".join(ev) + "\nEND:VCALENDAR\n"
    return body.replace("\n", "\r\n")
