"""Run approved action items: Calendar events, then one Slack summary per meeting (spec 8.5).

Idempotent by design, because Streamlit reruns the page on every interaction:
- the store is re-read on every call and finished parts are skipped;
- calendar event ids come from item ids, so a lost write is recovered from
  the 409 conflict instead of creating a second event;
- the Slack ts is stored right after posting, with no UI call in between.
With DRY_RUN the same bookkeeping uses "dryrun:" ids, so a second click shows
"skipped" just like a real run; a real run ignores those ids.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import metrics
from ..config import Settings, get_settings
from ..integrations.gcal import CalendarClient, CalendarError
from ..integrations.slack import SlackError, post_message
from ..meeting import render
from ..meeting.models import ActionItem, ExecutionResult, Meeting
from ..store.base import NotFoundError, StateStore

DRY_RUN_PREFIX = "dryrun:"
# Public-dataset excerpts are for extraction and evaluation only (see DECISIONS.md).
EXTERNAL_SOURCE_PREFIXES = ("assembly_",)

logger = logging.getLogger(__name__)

SlackPoster = Callable[..., str]


@dataclass
class ExecutionReport:
    meeting_id: str
    dry_run: bool
    results: list[ExecutionResult]
    calendar_payloads: list[dict[str, Any]] = field(default_factory=list)
    slack_message: render.SlackMessage | None = None
    slack_thread_reply: bool = False
    warnings: list[str] = field(default_factory=list)
    latency_ms: int = 0

    @property
    def failed(self) -> bool:
        return any(r.calendar == "failed" or r.slack == "failed" for r in self.results)


def is_done(value: str | None, dry_run: bool) -> bool:
    """A recorded id counts as done; dry-run ids only count in dry-run mode."""
    if not value:
        return False
    if value.startswith(DRY_RUN_PREFIX):
        return dry_run
    return True


def is_external_source(meeting: Meeting) -> bool:
    return (meeting.source_filename or "").startswith(EXTERNAL_SOURCE_PREFIXES)


def _calendar_step(
    items: list[ActionItem],
    meeting: Meeting,
    store: StateStore,
    settings: Settings,
    dry_run: bool,
    calendar: CalendarClient | None,
    outcome: dict[str, dict[str, str]],
    payloads: list[dict[str, Any]],
) -> None:
    setup_error: str | None = None
    for item in items:
        state = outcome[item.item_id]
        if is_done(item.calendar_event_id, dry_run):
            state["calendar"] = "skipped_existing"
            continue
        payload = render.calendar_event(item, meeting, settings.timezone)
        payloads.append(payload)
        if dry_run:
            store.update_item(item.item_id, calendar_event_id=DRY_RUN_PREFIX + payload["id"])
            state["calendar"] = "dry_run"
            logger.info("DRY_RUN calendar event %s on %s", payload["id"], payload["start"])
            continue
        if calendar is None and setup_error is None:
            try:
                calendar = CalendarClient.from_settings(settings)
            except CalendarError as exc:
                setup_error = str(exc)
        try:
            if calendar is None:
                raise CalendarError(setup_error or "캘린더를 사용할 수 없습니다.")
            event_id, status = calendar.insert_event(payload)
        except CalendarError as exc:
            state["calendar"], state["calendar_error"] = "failed", str(exc)
            continue
        store.update_item(item.item_id, calendar_event_id=event_id)
        state["calendar"] = status


def _slack_step(
    meeting_id: str,
    store: StateStore,
    settings: Settings,
    dry_run: bool,
    slack_poster: SlackPoster | None,
    outcome: dict[str, dict[str, str]],
    report: ExecutionReport,
) -> None:
    meeting = store.get_meeting(meeting_id)
    items = store.list_items(meeting_id=meeting_id, status="approved")
    pending = [i for i in items if not is_done(i.slack_ts, dry_run)]
    pending_ids = {i.item_id for i in pending}
    for item in items:
        if item.item_id not in pending_ids:
            outcome[item.item_id]["slack"] = "skipped_existing"
    if not pending:
        return

    followup = is_done(meeting.slack_ts, dry_run)
    message = (
        render.slack_followup(meeting, pending) if followup else render.slack_summary(meeting, items)
    )
    report.slack_message, report.slack_thread_reply = message, followup
    if dry_run:
        ts = DRY_RUN_PREFIX + uuid.uuid4().hex[:12]
        logger.info("DRY_RUN slack message for meeting %s (%d items)", meeting_id, len(pending))
    else:
        try:
            if slack_poster is None:  # the real sender needs a token and a channel
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
            ts = (slack_poster or post_message)(
                settings.slack_channel_id or "",
                message.text,
                message.blocks,
                thread_ts=meeting.slack_ts if followup else None,
            )
        except SlackError as exc:
            for item in pending:
                outcome[item.item_id]["slack"] = "failed"
                outcome[item.item_id]["slack_error"] = str(exc)
            return
    # Record right away: a rerun must not post the same message twice.
    if not followup:
        store.update_meeting(meeting_id, slack_ts=ts)
    for item in pending:
        store.update_item(item.item_id, slack_ts=ts)
        outcome[item.item_id]["slack"] = "dry_run" if dry_run else "sent"


def execute_meeting(
    meeting_id: str,
    *,
    store: StateStore,
    settings: Settings | None = None,
    calendar: CalendarClient | None = None,
    slack_poster: SlackPoster | None = None,
    dry_run: bool | None = None,
) -> ExecutionReport:
    """Execute every approved item of a meeting. Safe to call again at any time."""
    settings = settings or get_settings()
    dry_run = settings.actions_dry_run if dry_run is None else dry_run
    started = time.perf_counter()
    meeting = store.get_meeting(meeting_id)
    if meeting is None:
        raise NotFoundError(meeting_id)

    report = ExecutionReport(meeting_id=meeting_id, dry_run=dry_run, results=[])
    if is_external_source(meeting) and not dry_run:
        dry_run = report.dry_run = True
        report.warnings.append("공개 데이터셋 회의록은 Calendar·Slack을 실행하지 않고 DRY_RUN으로만 처리합니다.")

    items = store.list_items(meeting_id=meeting_id, status="approved")
    outcome: dict[str, dict[str, str]] = {i.item_id: {} for i in items}
    _calendar_step(items, meeting, store, settings, dry_run, calendar, outcome, report.calendar_payloads)
    _slack_step(meeting_id, store, settings, dry_run, slack_poster, outcome, report)

    for item in items:
        state = outcome[item.item_id]
        errors = [
            f"{label}: {state[key]}"
            for label, key in (("Calendar", "calendar_error"), ("Slack", "slack_error"))
            if key in state
        ]
        report.results.append(
            ExecutionResult(
                item_id=item.item_id,
                calendar=state.get("calendar", "skipped_existing"),
                slack=state.get("slack", "skipped_existing"),
                error=" / ".join(errors) or None,
            )
        )
    report.latency_ms = round((time.perf_counter() - started) * 1000)
    metrics.log_event(
        metrics.ACTIONS_EXECUTED,
        items=len(report.results),
        calendar_created=sum(r.calendar == "created" for r in report.results),
        calendar_skipped=sum(r.calendar == "skipped_existing" for r in report.results),
        calendar_failed=sum(r.calendar == "failed" for r in report.results),
        slack_sent=sum(r.slack == "sent" for r in report.results),
        slack_failed=sum(r.slack == "failed" for r in report.results),
        latency_ms=report.latency_ms,
        dry_run=dry_run,
    )
    return report
