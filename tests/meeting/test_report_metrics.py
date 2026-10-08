"""report_metrics.py: --since leaves out development events; DRY_RUN is counted apart."""

import importlib.util
import json
import sys

import pytest

from onboarding_agent.config import REPO_ROOT


@pytest.fixture(scope="module")
def report():
    spec = importlib.util.spec_from_file_location("report_metrics", REPO_ROOT / "scripts" / "report_metrics.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["report_metrics"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("report_metrics", None)


EVENTS = [
    {"ts": "2026-10-08T15:00:00+09:00", "event": "meeting_extracted", "items": 9, "unconfirmed": 9, "needs_review": 0,
     "path": "rule_based", "latency_ms": 100},  # development
    {"ts": "2026-10-12T10:00:00+09:00", "event": "meeting_extracted", "items": 4, "unconfirmed": 1, "needs_review": 1,
     "path": "function_calling", "latency_ms": 12000},
    {"ts": "2026-10-12T10:05:00+09:00", "event": "meeting_approved", "approved": 4, "excluded_unconfirmed": 0,
     "modified_ratio": 0.1},
    {"ts": "2026-10-12T10:06:00+09:00", "event": "actions_executed", "calendar_created": 4, "calendar_failed": 0,
     "slack_sent": 4, "slack_failed": 0, "dry_run": False},
    {"ts": "2026-10-12T10:07:00+09:00", "event": "actions_executed", "calendar_created": 0, "slack_sent": 0,
     "dry_run": True},
    {"ts": "2026-10-13T09:01:00+09:00", "event": "reminders_sent", "sent": {"D-1": 2, "D-day": 0, "overdue": 0},
     "failed": 0, "dry_run": False},
    {"ts": "2026-10-13T09:02:00+09:00", "event": "reminders_sent", "sent": {"D-1": 5, "D-day": 0, "overdue": 0},
     "failed": 0, "dry_run": True},
]


def test_since_leaves_out_development_events(report, tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in EVENTS) + "not json\n", encoding="utf-8")
    assert report.main(["--since", "2026-10-12", "--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "이벤트 6건" in out
    assert "| 처리한 회의록 | 1건 (function calling 1 · 구조화 출력 0 · 규칙 기반 0) |" in out
    assert "| 미확정 포함 항목 | 25% |" in out
    assert "Calendar 생성 4 · 실패 0 / Slack 발송 4 · 실패 0 (DRY_RUN 실행 1회 제외)" in out
    assert "D-1 2 · D-day 0 · 기한 지남 0 (DRY_RUN 실행 1회 제외)" in out
    assert out.count("(측정 필요)") == 4  # the "before" column is never filled in


def test_until_and_bad_input(report, tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in EVENTS), encoding="utf-8")
    assert report.main(["--until", "2026-10-08", "--log", str(log)]) == 0
    assert "이벤트 1건" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        report.main(["--since", "next week", "--log", str(log)])
