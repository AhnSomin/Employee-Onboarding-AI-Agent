"""Summarize logs/events.jsonl as Markdown for the README before/after table (spec 13).

Run with:
    uv run python scripts/report_metrics.py [--since 2026-10-12] [--until 2026-10-14T18:00] [--log FILE]

--since/--until take an ISO date or time (read in TIMEZONE without an offset) so
events from development and testing can be left out. DRY_RUN executions and
reminders are counted apart from real ones. The "before" column is measured by
people (docs/meeting/MEASUREMENT.md); this script never fills it in.
Sections: 기능 2 (회의록 → 액션). Add other features' sections to SECTIONS.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, tzinfo
from pathlib import Path

from onboarding_agent import metrics
from onboarding_agent.config import get_settings

NOT_MEASURED = "(측정 필요)"


def parse_moment(value: str, tz: tzinfo, *, end: bool = False) -> datetime:
    """'2026-10-12' (start of day, or end of day for --until) or '2026-10-12T09:00[+09:00]'."""
    if len(value) == 10:
        day = date.fromisoformat(value)
        return datetime.combine(day, time.max if end else time.min, tzinfo=tz)
    moment = datetime.fromisoformat(value)
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment


def load_events(path: Path, since: datetime | None, until: datetime | None) -> list[dict]:
    events = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
            moment = datetime.fromisoformat(event["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if (since and moment < since) or (until and moment > until):
            continue
        events.append(event)
    return events


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pct(part: float, whole: float) -> str:
    return f"{part / whole:.0%}" if whole else "해당 없음"


def meeting_section(events: list[dict]) -> list[str]:
    """기능 2: extraction, review, execution and reminders."""
    by_type: dict[str, list[dict]] = {}
    for event in events:
        by_type.setdefault(event.get("event", ""), []).append(event)
    extracted = by_type.get(metrics.MEETING_EXTRACTED, [])
    approved = by_type.get(metrics.MEETING_APPROVED, [])
    executed = by_type.get(metrics.ACTIONS_EXECUTED, [])
    reminders = by_type.get(metrics.REMINDERS_SENT, [])

    items = sum(e.get("items", 0) for e in extracted)
    paths = Counter(e.get("path") for e in extracted)
    latency = _avg([e["latency_ms"] for e in extracted if "latency_ms" in e])
    real_runs = [e for e in executed if not e.get("dry_run")]
    dry_runs = len(executed) - len(real_runs)
    sent = Counter()
    for event in reminders:
        if not event.get("dry_run"):
            sent.update(event.get("sent") or {})
    dry_reminders = sum(1 for e in reminders if e.get("dry_run"))
    modified = _avg([e["modified_ratio"] for e in approved if "modified_ratio" in e])

    lines = [
        "### 기능 2 — 회의록 → 액션",
        "",
        "| 지표 | 값 |",
        "|---|---|",
        f"| 처리한 회의록 | {len(extracted)}건 (function calling {paths['function_calling']} · 구조화 출력 "
        f"{paths['structured']} · 규칙 기반 {paths['rule_based']}) |",
        f"| 회의당 추출 항목 | {'해당 없음' if not extracted else f'평균 {items / len(extracted):.1f}개'} |",
        f"| 미확정 포함 항목 | {_pct(sum(e.get('unconfirmed', 0) for e in extracted), items)} |",
        f"| 검토 필요 항목 | {_pct(sum(e.get('needs_review', 0) for e in extracted), items)} |",
        f"| 평균 추출 지연 | {'해당 없음' if latency is None else f'{latency / 1000:.1f}초'} |",
        f"| 승인한 회의 | {len(approved)}건, 승인 항목 {sum(e.get('approved', 0) for e in approved)}개, "
        f"미확정이라 뺀 항목 {sum(e.get('excluded_unconfirmed', 0) for e in approved)}개 |",
        f"| 사람이 고친 셀 비율 | {'해당 없음' if modified is None else f'평균 {modified:.0%}'} |",
        f"| 실행 (실제) | Calendar 생성 {sum(e.get('calendar_created', 0) for e in real_runs)} · 실패 "
        f"{sum(e.get('calendar_failed', 0) for e in real_runs)} / Slack 발송 "
        f"{sum(e.get('slack_sent', 0) for e in real_runs)} · 실패 {sum(e.get('slack_failed', 0) for e in real_runs)}"
        f" (DRY_RUN 실행 {dry_runs}회 제외) |",
        f"| 리마인더 (실제) | D-1 {sent['D-1']} · D-day {sent['D-day']} · 기한 지남 {sent['overdue']}"
        f" (DRY_RUN 실행 {dry_reminders}회 제외) |",
        "",
        "#### README 적용 전/후 비교표",
        "",
        "적용 전 값은 docs/meeting/MEASUREMENT.md 절차로 사람이 직접 잰다.",
        "",
        "| 항목 | 적용 전 (수작업) | 적용 후 |",
        "|---|---|---|",
        f"| 회의록 1건을 정리해 일정 등록·공유까지 걸린 시간 | {NOT_MEASURED} | 추출 "
        f"{'해당 없음' if latency is None else f'{latency / 1000:.1f}초'} + 검토 시간 {NOT_MEASURED} |",
        f"| 담당자·기한이 빠진 항목 | {NOT_MEASURED} | 미확정 표시 "
        f"{_pct(sum(e.get('unconfirmed', 0) for e in extracted), items)} (사람이 채움) |",
        f"| 기한 리마인드 | {NOT_MEASURED} | 자동 {sum(sent.values())}건 |",
    ]
    return lines


SECTIONS: list[Callable[[list[dict]], list[str]]] = [meeting_section]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="이벤트 로그를 README용 지표 표로 정리합니다.")
    parser.add_argument("--since", help="이 시각 이후 이벤트만 (예: 2026-10-12 또는 2026-10-12T09:00)")
    parser.add_argument("--until", help="이 시각까지의 이벤트만")
    parser.add_argument("--log", type=Path, default=metrics.LOG_PATH, help="이벤트 로그 파일")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    tz = get_settings().tz
    try:
        since = parse_moment(args.since, tz) if args.since else None
        until = parse_moment(args.until, tz, end=True) if args.until else None
    except ValueError:
        parser.error("--since/--until 형식을 확인하세요 (예: 2026-10-12 또는 2026-10-12T09:00)")
    events = load_events(args.log, since, until)
    span = f"{args.since or '처음'} ~ {args.until or '지금'}"
    print(f"## 지표 — {span}, 이벤트 {len(events)}건\n")
    for section in SECTIONS:
        print("\n".join(section(events)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
