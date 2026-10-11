import json

import pytest

from onboarding_agent import metrics


def test_log_event_appends_jsonl_with_seoul_timestamp():
    metrics.log_event(metrics.MEETING_EXTRACTED, input_chars=1200, items=3, model="m", fallback=False)
    metrics.log_event(metrics.ACTIONS_EXECUTED, dry_run=True)

    lines = metrics.LOG_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "meeting_extracted"
    assert first["items"] == 3
    assert first["fallback"] is False
    assert first["ts"].endswith("+09:00")


def test_long_strings_are_cut():
    record = metrics.log_event("test", note="가" * 500)
    assert len(record["note"]) == metrics.MAX_STR_LEN + 1


def test_reserved_keys_are_rejected():
    with pytest.raises(ValueError):
        metrics.log_event("test", ts="now")


def test_write_failure_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(metrics, "LOG_PATH", blocker / "events.jsonl")
    assert metrics.log_event("test", ok=True)["ok"] is True
