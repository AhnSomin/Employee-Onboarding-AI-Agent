"""Send the reminders that are due for approved action items (spec 10.3).

Run with:
    uv run python scripts/run_reminders.py [--now 2026-10-15T09:00:00+09:00] [--dry-run]
    PYTHONPATH=src python scripts/run_reminders.py ...   # with only requirements-batch.txt

--now      reference time, ISO 8601; without an offset it is read in TIMEZONE. Default: now.
--dry-run  print the messages instead of sending them (also when ACTIONS_DRY_RUN=true).
GitHub Actions runs this on weekdays at 09:00 KST (.github/workflows/reminders.yml).
No roster is needed: Slack IDs are stored on the items.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, tzinfo

from onboarding_agent.config import ConfigError, get_settings
from onboarding_agent.meeting.dates import format_due, weekday_label
from onboarding_agent.meeting.render import STAGE_LABELS
from onboarding_agent.scheduler.reminders import STAGES, send_reminders
from onboarding_agent.store import StoreUnavailable, get_store


def parse_now(value: str | None, tz: tzinfo) -> datetime:
    if not value:
        return datetime.now(tz)
    moment = datetime.fromisoformat(value)
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="승인된 액션 아이템의 리마인더를 보냅니다.")
    parser.add_argument("--now", help="기준 시각 (ISO 8601, 예: 2026-10-15T09:00:00+09:00)")
    parser.add_argument("--dry-run", action="store_true", help="Slack으로 보내지 않고 메시지만 출력")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    try:
        settings = get_settings()
        store = get_store(settings)
    except (ConfigError, StoreUnavailable) as exc:
        print(f"[FAIL] 설정 또는 상태 저장소 — {exc}")
        return 1
    try:
        now = parse_now(args.now, settings.tz)
    except ValueError:
        parser.error(f"--now 형식을 확인하세요 (예: 2026-10-15T09:00:00+09:00): {args.now}")
    dry_run = args.dry_run or settings.actions_dry_run

    report = send_reminders(store, now, settings=settings, dry_run=dry_run)
    mode = "DRY_RUN" if dry_run else "실제 발송"
    print(f"리마인더 — 기준 {now:%Y-%m-%d %H:%M}({weekday_label(now.date())}) {now.tzname()} · {mode}")
    if not report.reminders:
        print("- 보낼 리마인더 없음")
    for reminder in report.reminders:
        item = reminder.item
        due = format_due(item.due_date, item.due_time) if item.due_date else "미정"
        where = "스레드 답글" if reminder.thread_ts else "새 메시지"
        flag = " · DRY_RUN(공개 데이터셋)" if reminder.dry_run and not dry_run else ""
        result = f"실패: {reminder.error}" if reminder.error else f"ts {reminder.ts}"
        print(f"- [{STAGE_LABELS[reminder.stage]}] {item.task} — {item.owner_name or '담당 미정'} "
              f"· 기한 {due} · {where}{flag} → {result}")
        if reminder.dry_run:
            print("  " + reminder.message.text.replace("\n", "\n  "))
    counts = ", ".join(f"{STAGE_LABELS[s]} {sum(r.stage == s for r in report.sent)}" for s in STAGES)
    print(f"결과: 보냄 {len(report.sent)}건 ({counts}), 실패 {len(report.failed)}건")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
