"""Clean up demo data this app created: Slack messages, calendar events, state rows.

Run with:
    uv run python scripts/reset_demo.py          # list what would be deleted (default)
    uv run python scripts/reset_demo.py --yes    # actually delete

Only marked data is touched:
- Slack: messages whose ts this app stored (the bot can delete only its own)
- Calendar: events with extendedProperties.private.created_by=onboarding-agent
- State: Sheets rows with created_by=onboarding-agent, or the local SQLite rows
"dryrun:" ids point at nothing outside and are only removed with the state rows.
"""

from __future__ import annotations

import argparse
import sys

from onboarding_agent.actions.executor import DRY_RUN_PREFIX
from onboarding_agent.config import Settings, get_settings
from onboarding_agent.integrations.gcal import CalendarClient, CalendarError
from onboarding_agent.integrations.slack import SlackError, delete_message, get_client
from onboarding_agent.store import StoreUnavailable, get_store


def _slack_targets(store) -> list[str]:
    stamps = [m.slack_ts for m in store.list_meetings()]
    stamps += [i.slack_ts for i in store.list_items()]
    return sorted({ts for ts in stamps if ts and not ts.startswith(DRY_RUN_PREFIX)})


def reset_slack(store, settings: Settings, apply: bool) -> str:
    targets = _slack_targets(store)
    if not targets:
        return "Slack: 지울 메시지 없음"
    if not apply:
        return f"Slack: 봇 메시지 {len(targets)}건 삭제 예정 (ts {', '.join(targets[:5])}{' …' if len(targets) > 5 else ''})"
    try:
        client = get_client(settings)
    except SlackError as exc:
        return f"Slack: 건너뜀 — {exc}"
    deleted = failed = 0
    for ts in targets:
        try:
            deleted += delete_message(settings.slack_channel_id or "", ts, client=client)
        except SlackError as exc:
            failed += 1
            print(f"  Slack {ts}: {exc}")
    return f"Slack: {deleted}건 삭제, 이미 없음 {len(targets) - deleted - failed}건, 실패 {failed}건"


def reset_calendar(settings: Settings, apply: bool) -> str:
    try:
        calendar = CalendarClient.from_settings(settings)
        events = calendar.list_app_events()
    except CalendarError as exc:
        return f"Calendar: 건너뜀 — {exc}"
    if not events:
        return "Calendar: 이 앱이 만든 일정 없음"
    if not apply:
        preview = ", ".join(
            f"{e.get('summary', '')[:20]}({(e.get('start') or {}).get('date') or (e.get('start') or {}).get('dateTime', '')[:10]})"
            for e in events[:5]
        )
        return f"Calendar: 이 앱이 만든 일정 {len(events)}개 삭제 예정 ({preview}{' …' if len(events) > 5 else ''})"
    deleted = failed = 0
    for event in events:
        try:
            deleted += calendar.delete_event(event["id"])
        except CalendarError as exc:
            failed += 1
            print(f"  Calendar {event['id']}: {exc}")
    return f"Calendar: {deleted}개 삭제, 실패 {failed}개"


def reset_state(store, settings: Settings, apply: bool) -> str:
    label = "Google Sheets 행" if settings.state_backend == "sheets" else "로컬 SQLite 행"
    counts = store.app_row_counts()
    summary = f"회의 {counts['meetings']}건, 액션 아이템 {counts['action_items']}건"
    if not apply:
        return f"{label}: {summary} 삭제 예정"
    deleted = store.delete_app_rows()
    return f"{label}: 회의 {deleted['meetings']}건, 액션 아이템 {deleted['action_items']}건 삭제"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="이 앱이 만든 데모 데이터를 정리합니다.")
    parser.add_argument("--yes", action="store_true", help="실제로 삭제합니다 (없으면 목록만 보여 줌)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    settings = get_settings()
    try:
        store = get_store(settings)
    except StoreUnavailable as exc:
        print(f"[FAIL] 상태 저장소 — {exc}")
        return 1

    print("삭제 실행" if args.yes else "미리보기 (삭제하려면 --yes)")
    # Slack first: its message ids live in the state rows deleted last.
    print("- " + reset_slack(store, settings, args.yes))
    print("- " + reset_calendar(settings, args.yes))
    print("- " + reset_state(store, settings, args.yes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
