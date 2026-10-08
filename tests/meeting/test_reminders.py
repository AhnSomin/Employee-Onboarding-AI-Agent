"""Reminder selection and sending (spec 10): stages, no duplicates, done items, DRY_RUN."""

import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from onboarding_agent import config, metrics
from onboarding_agent.config import REPO_ROOT, get_settings
from onboarding_agent.integrations.slack import SlackError
from onboarding_agent.meeting.models import ActionItem, Meeting, ReminderLog
from onboarding_agent.meeting.render import reminder_message
from onboarding_agent.scheduler.reminders import (
    last_stage,
    plan_reminders,
    previous_workday,
    reached_stage,
    select_reminders,
    send_reminders,
)
from onboarding_agent.store.sqlite_store import SqliteStore

KST = ZoneInfo("Asia/Seoul")
MON, TUE, WED = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
THU, FRI, SAT = date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)


def at(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=KST)


def item(item_id: str = "a", due: date | None = MON, **fields) -> ActionItem:
    data = {
        "item_id": item_id,
        "meeting_id": "m1",
        "task": f"할 일 {item_id}",
        "owner_name": "김민준",
        "owner_slack_id": "U00000001",
        "owner_status": "confirmed",
        "due_date": due,
        "due_status": "confirmed" if due else "unconfirmed",
        "evidence_quote": "근거",
        "status": "approved",
    }
    data.update(fields)
    return ActionItem(**data)


def sent(stage: str, ts: str = "1760000000.000100") -> ReminderLog:
    return ReminderLog(stage=stage, sent_at=at(THU), ts=ts)


# --- stages ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("due", "today", "stage"),
    [
        (MON, THU, None),
        (MON, FRI, "D-1"),  # Friday is the working day before a Monday due date
        (MON, SAT, "D-1"),  # still the D-1 stage if someone runs it on Saturday
        (MON, MON, "D-day"),
        (MON, TUE, "overdue"),
        (WED, TUE, "D-1"),
        (WED, MON, None),
        (SAT, FRI, "D-1"),  # a weekend due date is reminded on Friday
    ],
)
def test_reached_stage(due, today, stage):
    assert reached_stage(due, today) == stage


def test_previous_workday_skips_the_weekend():
    assert previous_workday(MON) == FRI
    assert previous_workday(WED) == TUE


# --- selection -----------------------------------------------------------------


def test_only_approved_items_with_a_due_date():
    items = [
        item("a"),
        item("b", status="done"),
        item("c", status="cancelled"),
        item("d", status="draft"),
        item("e", due=None),
    ]
    assert [(i.item_id, s) for i, s in select_reminders(items, at(FRI))] == [("a", "D-1")]


def test_each_stage_once_and_reruns_send_nothing():
    reminded = item(reminders=[sent("D-1")])
    assert select_reminders([reminded], at(FRI)) == []
    assert [s for _, s in select_reminders([reminded], at(MON))] == ["D-day"]
    overdue_sent = item(reminders=[sent("D-1"), sent("D-day"), sent("overdue")])
    assert select_reminders([overdue_sent], at(date(2026, 10, 20))) == []


def test_a_missed_stage_sends_only_the_highest_one():
    assert [s for _, s in select_reminders([item()], at(MON))] == ["D-day"]
    assert [s for _, s in select_reminders([item()], at(WED))] == ["overdue"]


def test_dry_run_records_count_only_in_dry_run():
    rehearsed = item(reminders=[sent("D-1", ts="dryrun:abc")])
    assert select_reminders([rehearsed], at(FRI), dry_run=True) == []
    assert [s for _, s in select_reminders([rehearsed], at(FRI))] == ["D-1"]
    assert last_stage(rehearsed) is None


def test_stage_set_without_a_log_is_respected():
    assert select_reminders([item(last_reminded_stage="D-day")], at(MON)) == []


def test_disabled_stage_is_not_sent():
    assert select_reminders([item()], at(FRI), stages=["D-day", "overdue"]) == []


def test_reminders_are_ordered_by_due_date():
    items = [item("late", due=date(2026, 10, 9)), item("early", due=date(2026, 10, 8))]
    assert [i.item_id for i, _ in select_reminders(items, at(FRI))] == ["early", "late"]


def test_reminder_message_follows_the_spec():
    message = reminder_message(item(due=date(2026, 10, 16)), "D-1")
    assert message.text.splitlines()[:2] == [
        "⏰ [D-1] 할 일 a — <@U00000001> 기한 10/16(금)",
        "완료했다면 앱의 '액션 현황'에서 완료 처리해 주세요.",
    ]
    unregistered = reminder_message(item(owner_slack_id=None, due_time=time(15)), "overdue")
    assert unregistered.text.startswith("⏰ [기한 지남] 할 일 a — 김민준(Slack 미등록) 기한 10/12(월) 15:00")


# --- sending -------------------------------------------------------------------


class Poster:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, channel, text, blocks=None, thread_ts=None):
        if self.fail:
            raise SlackError("채널을 찾을 수 없습니다.", "channel_not_found")
        self.calls.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return f"1760000000.{len(self.calls):06d}"


@pytest.fixture
def store(monkeypatch) -> SqliteStore:
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C0TEST")
    store = SqliteStore(get_settings().sqlite_path)
    store.save_meeting(
        Meeting(
            meeting_id="m1",
            title="주간 회의",
            meeting_date=THU,
            slack_ts="1759900000.000001",
            created_at=at(THU),
        )
    )
    store.upsert_items([item("a", due=MON), item("b", due=WED), item("c", due=FRI, status="done")])
    return store


def send(store, now, *, dry_run=False, poster=None):
    return send_reminders(
        store, now, settings=get_settings(), dry_run=dry_run, poster=poster, clock=lambda: at(now.date(), 10)
    )


def test_send_replies_in_the_meeting_thread_once(store):
    poster = Poster()
    report = send(store, at(FRI), poster=poster)
    assert [(r.item.item_id, r.stage) for r in report.sent] == [("a", "D-1")]  # done item c is skipped
    text = reminder_message(store.get_item("a"), "D-1").text
    assert poster.calls == [{"channel": "C0TEST", "text": text, "thread_ts": "1759900000.000001"}]
    saved = store.get_item("a")
    assert saved.last_reminded_stage == "D-1"
    assert saved.reminders == [ReminderLog(stage="D-1", sent_at=at(FRI, 10), ts="1760000000.000001")]
    assert send(store, at(FRI), poster=poster).reminders == []  # same time again: nothing
    assert len(poster.calls) == 1


def test_overdue_and_next_items_follow_on_later_days(store):
    poster = Poster()
    for day in (FRI, MON, TUE, TUE, WED, date(2026, 10, 20)):
        send(store, at(day), poster=poster)
    stages = {i.item_id: [log.stage for log in i.reminders] for i in store.list_items()}
    assert stages == {"a": ["D-1", "D-day", "overdue"], "b": ["D-1", "D-day", "overdue"], "c": []}
    assert len(poster.calls) == 6


def test_dry_run_never_posts_and_does_not_block_a_real_run(store):
    poster = Poster()
    dry = send(store, at(FRI), dry_run=True, poster=poster)
    assert poster.calls == [] and dry.sent[0].ts.startswith("dryrun:")
    assert send(store, at(FRI), dry_run=True, poster=poster).reminders == []
    assert [r.stage for r in send(store, at(FRI), poster=poster).sent] == ["D-1"]
    assert len(poster.calls) == 1


def test_dry_run_summary_is_not_a_thread_for_a_real_reminder(store):
    store.update_meeting("m1", slack_ts="dryrun:summary")
    poster = Poster()
    send(store, at(FRI), poster=poster)
    assert poster.calls[0]["thread_ts"] is None


def test_failed_send_is_not_recorded_and_is_retried(store):
    report = send(store, at(FRI), poster=Poster(fail=True))
    assert report.failed and report.failed[0].error == "채널을 찾을 수 없습니다."
    assert store.get_item("a").reminders == []
    assert [r.stage for r in send(store, at(FRI), poster=Poster()).sent] == ["D-1"]


def test_missing_slack_settings_fail_without_calling_slack(store, monkeypatch):
    monkeypatch.delenv("SLACK_CHANNEL_ID")
    config.reset_settings_cache()
    report = send(store, at(FRI))
    assert "SLACK_BOT_TOKEN" in report.failed[0].error and "SLACK_CHANNEL_ID" in report.failed[0].error


def test_public_dataset_meetings_stay_dry_run(store):
    store.update_meeting("m1", source_filename="assembly_dev_sample.txt")
    poster = Poster()
    report = send(store, at(FRI), poster=poster)
    assert poster.calls == [] and report.sent[0].dry_run


def test_preview_sends_and_records_nothing(store):
    plan = plan_reminders(store, at(FRI), settings=get_settings(), dry_run=False)
    assert [(r.item.item_id, r.stage) for r in plan] == [("a", "D-1")]
    assert store.get_item("a").reminders == []


def test_metrics_event_counts_stages(store):
    send(store, at(FRI), poster=Poster())
    events = [json.loads(line) for line in metrics.LOG_PATH.read_text(encoding="utf-8").splitlines()]
    event = events[-1]
    assert event["event"] == "reminders_sent"
    assert event["sent"] == {"D-1": 1, "D-day": 0, "overdue": 0}
    assert (event["failed"], event["dry_run"]) == (0, False)


# --- command line ----------------------------------------------------------------


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("run_reminders", REPO_ROOT / "scripts" / "run_reminders.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_reminders"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("run_reminders", None)


def test_cli_dry_run_with_now(cli, store, capsys):
    assert cli.main(["--now", "2026-10-09T09:00:00+09:00", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "기준 2026-10-09 09:00(금) KST · DRY_RUN" in out
    assert "[D-1] 할 일 a — 김민준 · 기한 10/12(월) · 스레드 답글 → ts dryrun:" in out
    assert "결과: 보냄 1건 (D-1 1, D-day 0, 기한 지남 0), 실패 0건" in out
    assert cli.main(["--now", "2026-10-09T09:00:00+09:00", "--dry-run"]) == 0
    assert "보낼 리마인더 없음" in capsys.readouterr().out


def test_cli_reads_a_naive_now_in_the_configured_timezone(cli):
    assert cli.parse_now("2026-10-09T09:00", KST) == at(FRI)
    assert cli.parse_now("2026-10-09T00:00:00+00:00", KST) == at(FRI)


def test_cli_rejects_a_bad_now(cli, store):
    with pytest.raises(SystemExit):
        cli.main(["--now", "next friday"])


def test_batch_imports_no_app_or_llm_code():
    code = (
        "import runpy, sys; runpy.run_path('scripts/run_reminders.py', run_name='probe'); "
        "print(sorted(m for m in ('streamlit', 'google.genai', 'googleapiclient', 'gspread') if m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"  # gspread loads only when STATE_BACKEND=sheets
