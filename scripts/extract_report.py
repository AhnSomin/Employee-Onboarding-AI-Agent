"""Run meeting extraction on text files and print Markdown tables (no labels needed).

Run with:
    uv run python scripts/extract_report.py FILE [FILE ...]

Extraction only: nothing is approved, saved, or sent to Calendar or Slack,
and no metrics events are written. The second table lists every confirmed
owner or due date with the evidence it rests on.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from onboarding_agent.config import get_settings
from onboarding_agent.meeting.dates import format_due
from onboarding_agent.meeting.extract import extract_meeting, load_prompt
from onboarding_agent.meeting.loader import load_meeting
from onboarding_agent.meeting.roster import load_roster

PATH_LABELS = {"function_calling": "function calling", "structured": "구조화 출력", "rule_based": "규칙 기반"}


def _cut(text: str, limit: int = 110) -> str:
    text = " ".join(text.split()).replace("|", "/")
    return text if len(text) <= limit else text[:limit] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    settings = get_settings()
    roster = load_roster(settings.roster_path)
    summary = [
        "| 파일 | 글자 수 | 경로 | 모델 | 턴 | 항목 | 확정(담당·기한 모두) | 담당 확정 | 기한 확정 | 미확정 포함 | 검토 필요 | 검증 경고 | 지연 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    confirmed_lines = [
        "| 파일 | 할 일 | 담당자 | 기한 | 근거 인용 |",
        "|---|---|---|---|---|",
    ]
    for path in args.files:
        loaded = load_meeting(data=path.read_bytes(), filename=path.name)
        started = time.perf_counter()
        result = extract_meeting(
            loaded.text,
            title=loaded.title_suggestion,
            meeting_date=loaded.meeting_date,
            source_filename=path.name,
            roster=roster,
            log_metrics=False,
        )
        seconds = time.perf_counter() - started
        items = result.action_items
        owner_ok = [i for i in items if i.owner_status == "confirmed"]
        due_ok = [i for i in items if i.due_status == "confirmed"]
        both = [i for i in items if i.owner_status == i.due_status == "confirmed"]
        summary.append(
            f"| {path.name} | {len(loaded.text):,} | {PATH_LABELS[result.extraction_path]} "
            f"| {result.model_used or '-'} | {result.tool_calls.turns if result.tool_calls else '-'} "
            f"| {len(items)} | {len(both)} | {len(owner_ok)} | {len(due_ok)} | {len(items) - len(both)} "
            f"| {sum(i.needs_review for i in items)} | {len(result.warnings)} | {seconds:.1f}초 |"
        )
        for item in items:
            if item.owner_status != "confirmed" and item.due_status != "confirmed":
                continue
            owner = item.owner_name if item.owner_status == "confirmed" else "미확정"
            due = format_due(item.due_date, item.due_time) if item.due_status == "confirmed" else "미확정"
            confirmed_lines.append(
                f"| {path.name} | {_cut(item.task, 50)} | {owner} | {due} | “{_cut(item.evidence_quote)}” |"
            )

    chain = " → ".join(settings.gemini_models) or "(모델 미지정)"
    print(f"### 추출 결과 — {chain} · 프롬프트 {load_prompt().version}\n")
    print("\n".join(summary))
    print("\n#### 확정된 담당자·기한과 근거\n")
    print("\n".join(confirmed_lines) if len(confirmed_lines) > 2 else "(확정된 담당자·기한 없음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
