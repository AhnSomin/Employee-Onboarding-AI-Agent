"""Executor idempotency and failure handling with fake Calendar and Slack."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from onboarding_agent import metrics
from onboarding_agent.actions.executor import DRY_RUN_PREFIX, execute_meeting
from onboarding_agent.config import Settings
from onboarding_agent.integrations.gcal import CalendarError
from onboarding_agent.integrations.slack import SlackError
from onboarding_agent.meeting.models import ActionItem, Meeting
from onboarding_agent.store.sheets_store import SheetsStore
from onboarding_agent.store.sqlite_store import SqliteStore

KST = ZoneInfo("Asia/Seoul")
SETTINGS = Settings(slack_channel_id="C1", actions_dry_run=False)


class FakeCalendar:
    """Remembers created ids like the real API: a second insert gets a 409."""

    def __init__(self, fail_times: int = 0):
        self.created: dict[str, dict] = {}
        self.insert_calls = 0
        self.fail_times = fail_times

    def insert_event(self, body):
        self.insert_calls += 1
        if self.fail_times:
            self.fail_times -= 1
            raise CalendarError("캘린더에 접근할 수 없습니다(403).")
        if body["id"] in self.created:
            return body["id"], "skipped_existing"
        self.created[body["id"]] = body
        return body["id"], "created"


class FakeSlack:
    def __init__(self, fail_times: int = 0):
        self.messages = []
        self.fail_times = fail_times

    def __call__(self, channel, text, blocks=None, thread_ts=None):
        if self.fail_times:
            self.fail_times -= 1
            raise SlackError("봇을 채널에 초대하세요.", "not_in_channel")
        ts = f"1700000000.{len(self.messages):06d}"
        self.messages.append({"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts})
        return ts


@pytest.fixture(params=["sqlite", "sheets-fake"])
def store(request, tmp_path, fake_spreadsheet):
    return SqliteStore(tmp_path / "state.db") if request.param == "sqlite" else SheetsStore(fake_spreadsheet)


def seed(store, *, count=2, source="01_structured_minutes.txt", approved=True) -> str:
    meeting = Meeting(
        meeting_id="m1",
        title="주간 회의",
        meeting_date=date(2026, 10, 8),
        source_filename=source,
        summary=["요약"],
        created_at=datetime(2026, 10, 8, 10, tzinfo=KST),
    )
    store.save_meeting(meeting)
    store.upsert_items([approved_item(n, approved) for n in range(count)])
    return meeting.meeting_id


def approved_item(n: int, approved: bool = True) -> ActionItem:
    return ActionItem(
        item_id=f"item-{n}",
        meeting_id="m1",
        task=f"할 일 {n}",
        owner_name="김민준",
        owner_slack_id="U1",
        owner_status="confirmed",
        due_date=date(2026, 10, 16),
        due_status="confirmed",
        evidence_quote="근거",
        status="approved" if approved else "draft",
        approved_at=datetime(2026, 10, 8, 11, tzinfo=KST) if approved else None,
    )


def run(store, calendar, slack, **kwargs):
    return execute_meeting("m1", store=store, settings=SETTINGS, calendar=calendar, slack_poster=slack, **kwargs)


def test_one_approval_creates_one_event_per_item_and_one_message(store):
    seed(store)
    calendar, slack = FakeCalendar(), FakeSlack()
    report = run(store, calendar, slack)
    assert [(r.calendar, r.slack) for r in report.results] == [("created", "sent")] * 2
    assert len(calendar.created) == 2 and len(slack.messages) == 1
    assert "할 일 0" in slack.messages[0]["text"] and "할 일 1" in slack.messages[0]["text"]
    assert store.get_meeting("m1").slack_ts == slack.messages[0]["ts"]
    assert all(i.calendar_event_id and i.slack_ts for i in store.list_items(meeting_id="m1"))


def test_running_again_creates_nothing_new(store):
    seed(store)
    calendar, slack = FakeCalendar(), FakeSlack()
    run(store, calendar, slack)
    again = run(store, calendar, slack)
    assert [(r.calendar, r.slack) for r in again.results] == [("skipped_existing", "skipped_existing")] * 2
    assert calendar.insert_calls == 2 and len(slack.messages) == 1


def test_lost_calendar_write_is_recovered_from_conflict(store):
    seed(store, count=1)
    calendar, slack = FakeCalendar(), FakeSlack()
    run(store, calendar, slack)
    store.update_item("item-0", calendar_event_id=None)  # as if the store write had been lost
    again = run(store, calendar, slack)
    assert again.results[0].calendar == "skipped_existing"
    assert len(calendar.created) == 1
    assert store.get_item("item-0").calendar_event_id in calendar.created


def test_partial_failure_then_retry_only_redoes_the_failed_part(store):
    seed(store)
    calendar, slack = FakeCalendar(), FakeSlack(fail_times=1)
    first = run(store, calendar, slack)
    assert [(r.calendar, r.slack) for r in first.results] == [("created", "failed")] * 2
    assert first.failed and "Slack: 봇을 채널에 초대하세요." in first.results[0].error

    retry = run(store, calendar, slack)
    assert [(r.calendar, r.slack) for r in retry.results] == [("skipped_existing", "sent")] * 2
    assert calendar.insert_calls == 2 and len(slack.messages) == 1


def test_calendar_failure_for_one_item_is_retried_alone(store):
    seed(store)
    calendar, slack = FakeCalendar(fail_times=1), FakeSlack()
    first = run(store, calendar, slack)
    assert [r.calendar for r in first.results] == ["failed", "created"]
    retry = run(store, calendar, slack)
    assert [r.calendar for r in retry.results] == ["created", "skipped_existing"]
    assert len(calendar.created) == 2


def test_items_approved_later_go_to_a_thread_reply(store):
    seed(store, count=1)
    calendar, slack = FakeCalendar(), FakeSlack()
    run(store, calendar, slack)
    store.upsert_items([approved_item(1)])
    report = run(store, calendar, slack)
    assert [r.slack for r in report.results] == ["skipped_existing", "sent"]
    assert report.slack_thread_reply
    assert slack.messages[1]["thread_ts"] == slack.messages[0]["ts"]
    assert slack.messages[1]["text"].startswith("➕ 추가로 승인된 할 일")


def test_drafts_are_never_executed(store):
    seed(store, count=1)
    store.upsert_items([approved_item(9, approved=False)])
    report = run(store, FakeCalendar(), FakeSlack())
    assert [r.item_id for r in report.results] == ["item-0"]


def test_dry_run_records_markers_and_skips_on_second_click(store):
    seed(store)
    calendar, slack = FakeCalendar(), FakeSlack()
    first = run(store, calendar, slack, dry_run=True)
    assert [(r.calendar, r.slack) for r in first.results] == [("dry_run", "dry_run")] * 2
    assert calendar.insert_calls == 0 and slack.messages == []
    assert len(first.calendar_payloads) == 2 and first.slack_message is not None
    assert all(i.calendar_event_id.startswith(DRY_RUN_PREFIX) for i in store.list_items())

    second = run(store, calendar, slack, dry_run=True)
    assert [(r.calendar, r.slack) for r in second.results] == [("skipped_existing", "skipped_existing")] * 2


def test_real_run_ignores_dry_run_markers(store):
    seed(store)
    calendar, slack = FakeCalendar(), FakeSlack()
    run(store, calendar, slack, dry_run=True)
    real = run(store, calendar, slack, dry_run=False)
    assert [(r.calendar, r.slack) for r in real.results] == [("created", "sent")] * 2
    assert slack.messages[0]["thread_ts"] is None  # a fresh summary, not a reply to the dry run


def test_public_dataset_meetings_never_reach_calendar_or_slack(store):
    seed(store, source="assembly_excerpt_01.txt")
    calendar, slack = FakeCalendar(), FakeSlack()
    report = run(store, calendar, slack, dry_run=False)
    assert report.dry_run is True
    assert calendar.insert_calls == 0 and slack.messages == []
    assert any("공개 데이터셋" in w for w in report.warnings)


def test_missing_configuration_fails_with_actionable_messages(store):
    seed(store, count=1)
    report = execute_meeting("m1", store=store, settings=Settings(actions_dry_run=False))
    result = report.results[0]
    assert (result.calendar, result.slack) == ("failed", "failed")
    assert "GCAL_CALENDAR_ID" in result.error and "SLACK_BOT_TOKEN" in result.error


def test_execution_is_logged_without_content(store):
    seed(store)
    run(store, FakeCalendar(), FakeSlack())
    record = json.loads(metrics.LOG_PATH.read_text(encoding="utf-8").splitlines()[-1])
    assert record["event"] == "actions_executed"
    assert (record["calendar_created"], record["slack_sent"], record["dry_run"]) == (2, 2, False)
    assert "할 일" not in json.dumps(record, ensure_ascii=False)
