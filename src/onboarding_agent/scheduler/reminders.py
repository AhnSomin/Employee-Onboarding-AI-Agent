"""Reminders for approved action items (spec 10).

`select_reminders` is pure: given the items and the reference time, it says
which stage each item is due for. Stages, low to high: D-1 (the last working
day before the due date, Mon–Fri; holidays are not considered), D-day, and
overdue (sent once). Only the highest stage reached is sent, and only when it
is higher than the last one sent: a batch that missed D-1 still sends D-day,
and a rerun sends nothing new.

`send_reminders` posts them, as thread replies to the meeting's summary when
there is one, and records each reminder right after sending it. DRY_RUN
records "dryrun:" message ids: a dry rerun is deduplicated the same way, and
a real run ignores dry-run records. Nothing here imports Streamlit, Gemini or
Calendar code, so the batch runs with requirements-batch.txt.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .. import metrics
from ..actions.dry_run import DRY_RUN_PREFIX, is_done, is_external_source
from ..config import Settings
from ..integrations.slack import SlackError, post_message
from ..meeting import render
from ..meeting.models import ActionItem, Meeting, ReminderLog, ReminderStage
from ..store.base import StateStore

STAGES: tuple[ReminderStage, ...] = ("D-1", "D-day", "overdue")

logger = logging.getLogger(__name__)

SlackPoster = Callable[..., str]


def previous_workday(day: date) -> date:
    """The last Mon–Fri before `day`: Friday for a Monday (or weekend) due date."""
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def reached_stage(due: date, today: date) -> ReminderStage | None:
    if today > due:
        return "overdue"
    if today == due:
        return "D-day"
    if today >= previous_workday(due):
        return "D-1"
    return None


def last_stage(item: ActionItem, *, dry_run: bool = False) -> ReminderStage | None:
    """The highest stage already sent. Dry-run records count only in dry-run mode."""
    if not item.reminders:
        return item.last_reminded_stage  # set by hand or before the reminder log existed
    sent = [log.stage for log in item.reminders if is_done(log.ts, dry_run)]
    return max(sent, key=STAGES.index, default=None)


def _due_key(item: ActionItem) -> tuple[date, time]:
    return item.due_date or date.max, item.due_time or time.min


def select_reminders(
    items: Iterable[ActionItem],
    now: datetime,
    stages: Iterable[str] = STAGES,
    *,
    dry_run: bool = False,
) -> list[tuple[ActionItem, ReminderStage]]:
    """Approved items that need a reminder at `now` (local time), with the stage, by due date."""
    enabled = set(stages)
    today = now.date()
    selected: list[tuple[ActionItem, ReminderStage]] = []
    for item in items:
        if item.status != "approved" or item.due_date is None:
            continue
        stage = reached_stage(item.due_date, today)
        if stage is None or stage not in enabled:
            continue
        done = last_stage(item, dry_run=dry_run)
        if done is not None and STAGES.index(stage) <= STAGES.index(done):
            continue
        selected.append((item, stage))
    return sorted(selected, key=lambda pair: _due_key(pair[0]))


@dataclass
class Reminder:
    item: ActionItem
    stage: ReminderStage
    message: render.SlackMessage
    thread_ts: str | None  # the meeting summary this replies to; None posts a new message
    dry_run: bool
    ts: str | None = None
    error: str | None = None


@dataclass
class ReminderReport:
    now: datetime
    dry_run: bool
    reminders: list[Reminder] = field(default_factory=list)

    @property
    def sent(self) -> list[Reminder]:
        return [r for r in self.reminders if r.ts and not r.error]

    @property
    def failed(self) -> list[Reminder]:
        return [r for r in self.reminders if r.error]


def plan_reminders(store: StateStore, now: datetime, *, settings: Settings, dry_run: bool) -> list[Reminder]:
    """What a run at `now` would send. Sends and records nothing (the status page preview)."""
    meetings: dict[str, Meeting] = {m.meeting_id: m for m in store.list_meetings()}
    items = store.list_items(status="approved")
    plan: list[Reminder] = []
    for external in (False, True):  # public-dataset meetings never reach Slack
        group = [
            item
            for item in items
            if (item.meeting_id in meetings and is_external_source(meetings[item.meeting_id])) == external
        ]
        mode = dry_run or external
        for item, stage in select_reminders(group, now, settings.reminder_stages, dry_run=mode):
            meeting = meetings.get(item.meeting_id)
            parent = meeting.slack_ts if meeting and is_done(meeting.slack_ts, mode) else None
            plan.append(Reminder(item, stage, render.reminder_message(item, stage), parent, mode))
    return sorted(plan, key=lambda r: _due_key(r.item))


def _post(reminder: Reminder, settings: Settings, poster: SlackPoster | None) -> str:
    if reminder.dry_run:
        logger.info("DRY_RUN reminder %s for item %s", reminder.stage, reminder.item.item_id)
        return DRY_RUN_PREFIX + uuid.uuid4().hex[:12]
    if poster is None:  # the real sender needs a token and a channel
        missing = [
            name
            for name, value in (
                ("SLACK_BOT_TOKEN", settings.slack_bot_token),
                ("SLACK_CHANNEL_ID", settings.slack_channel_id),
            )
            if not value
        ]
        if missing:
            raise SlackError("설정이 필요합니다: " + ", ".join(missing), "not_configured")
    return (poster or post_message)(
        settings.slack_channel_id or "",
        reminder.message.text,
        reminder.message.blocks,
        thread_ts=reminder.thread_ts,
    )


def send_reminders(
    store: StateStore,
    now: datetime,
    *,
    settings: Settings,
    dry_run: bool,
    poster: SlackPoster | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ReminderReport:
    """Send every reminder due at `now` and record it. Safe to run again at any time."""
    clock = clock or (lambda: datetime.now(settings.tz))
    plan = plan_reminders(store, now, settings=settings, dry_run=dry_run)
    report = ReminderReport(now=now, dry_run=dry_run, reminders=plan)
    for reminder in report.reminders:
        try:
            reminder.ts = _post(reminder, settings, poster)
        except SlackError as exc:
            reminder.error = str(exc)
            continue
        # Record right away: a rerun must not send the same reminder twice.
        log = ReminderLog(stage=reminder.stage, sent_at=clock(), ts=reminder.ts)
        current = store.get_item(reminder.item.item_id) or reminder.item
        store.update_item(
            current.item_id,
            reminders=[*current.reminders, log],
            last_reminded_stage=reminder.stage,
            last_reminded_at=log.sent_at,
        )
    by_stage = Counter(r.stage for r in report.sent)
    metrics.log_event(
        metrics.REMINDERS_SENT,
        sent={stage: by_stage[stage] for stage in STAGES},
        failed=len(report.failed),
        dry_run=dry_run,
        forced_dry_run=sum(1 for r in report.reminders if r.dry_run and not dry_run),
    )
    return report
