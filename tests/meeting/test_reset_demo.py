"""reset_demo.py lists by default and deletes only with --yes (here: a tmp SQLite store)."""

import importlib.util
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from onboarding_agent.config import REPO_ROOT, get_settings
from onboarding_agent.meeting.models import ActionItem, Meeting
from onboarding_agent.store.sqlite_store import SqliteStore


@pytest.fixture(scope="module")
def reset_demo():
    spec = importlib.util.spec_from_file_location("reset_demo", REPO_ROOT / "scripts" / "reset_demo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["reset_demo"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("reset_demo", None)


def seed() -> SqliteStore:
    store = SqliteStore(get_settings().sqlite_path)
    store.save_meeting(
        Meeting(meeting_id="m1", title="t", meeting_date=date(2026, 10, 8), slack_ts="1700000000.000001",
                created_at=datetime(2026, 10, 8, tzinfo=ZoneInfo("Asia/Seoul")))
    )
    store.upsert_items(
        [
            ActionItem(item_id="a", meeting_id="m1", task="x", evidence_quote="q", slack_ts="1700000000.000001"),
            ActionItem(item_id="b", meeting_id="m1", task="y", evidence_quote="q", slack_ts="dryrun:abc"),
        ]
    )
    return store


def test_default_run_only_lists(reset_demo, capsys):
    store = seed()
    assert reset_demo.main([]) == 0
    out = capsys.readouterr().out
    assert "미리보기 (삭제하려면 --yes)" in out
    assert "Slack: 봇 메시지 1건 삭제 예정" in out  # dry-run ids are not Slack messages
    assert "Calendar: 건너뜀" in out  # not configured
    assert "로컬 SQLite 행: 회의 1건, 액션 아이템 2건 삭제 예정" in out
    assert store.app_row_counts() == {"meetings": 1, "action_items": 2}


def test_yes_deletes_marked_state_and_skips_unconfigured_services(reset_demo, capsys):
    store = seed()
    assert reset_demo.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "Slack: 건너뜀" in out
    assert "로컬 SQLite 행: 회의 1건, 액션 아이템 2건 삭제" in out
    assert store.app_row_counts() == {"meetings": 0, "action_items": 0}
