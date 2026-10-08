"""Meeting feature models (spec section 7).

LLM output models carry content only. Domain models add IDs, status and
validation results, which code fills in, never the LLM.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, model_validator

FieldStatus = Literal["confirmed", "unconfirmed"]
ItemStatus = Literal["draft", "approved", "done", "cancelled"]
ReminderStage = Literal["D-1", "D-day", "overdue"]


def new_id() -> str:
    return str(uuid.uuid4())


# --- LLM output only ---


class LLMDecision(BaseModel):
    text: str
    evidence_quote: str


class LLMActionItem(BaseModel):
    task: str  # one sentence ending in "~하기"
    owner_name: str | None = None  # only when the minutes name someone explicitly
    co_owners: list[str] = []  # P1
    due_text: str | None = None  # due expression exactly as written in the minutes
    due_date_guess: str | None = None  # YYYY-MM-DD, reference only; code decides
    due_time_guess: str | None = None  # HH:MM, reference only
    evidence_quote: str  # verbatim quote from the minutes


class LLMExtraction(BaseModel):
    title_suggestion: str | None = None
    summary: list[str]  # 3-5 bullets
    decisions: list[LLMDecision]
    action_items: list[LLMActionItem]
    open_issues: list[str] = []  # P1


# --- Domain ---


class Decision(BaseModel):
    decision_id: str
    text: str
    evidence_quote: str
    needs_review: bool = False


class ActionItem(BaseModel):
    item_id: str  # uuid4 assigned at extraction, immutable afterwards
    meeting_id: str
    task: str
    owner_name: str | None = None
    co_owners: list[str] = []
    owner_slack_id: str | None = None
    owner_status: FieldStatus = "unconfirmed"
    due_date: date | None = None
    due_time: time | None = None
    due_text: str | None = None
    due_status: FieldStatus = "unconfirmed"
    evidence_quote: str
    needs_review: bool = False
    review_notes: list[str] = []  # validator reasons shown in the UI
    status: ItemStatus = "draft"
    calendar_event_id: str | None = None
    slack_ts: str | None = None
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    last_reminded_stage: ReminderStage | None = None
    last_reminded_at: datetime | None = None

    @model_validator(mode="after")
    def _confirmed_fields_have_values(self) -> ActionItem:
        # A field may only be "confirmed" when it actually holds a value.
        if self.owner_status == "confirmed" and not (self.owner_name or "").strip():
            raise ValueError("담당자가 비어 있으면 확정할 수 없습니다.")
        if self.due_status == "confirmed" and self.due_date is None:
            raise ValueError("기한 날짜가 없으면 확정할 수 없습니다.")
        return self


class Meeting(BaseModel):
    meeting_id: str
    title: str
    meeting_date: date
    source_filename: str | None = None
    summary: list[str] = []
    decisions: list[Decision] = []
    open_issues: list[str] = []
    slack_ts: str | None = None  # summary message ts, parent for thread replies
    created_at: datetime


class ExtractionResult(BaseModel):
    meeting: Meeting
    action_items: list[ActionItem]
    model_used: str | None
    fallback_used: bool
    warnings: list[str] = []


class ExecutionResult(BaseModel):
    item_id: str
    calendar: Literal["created", "skipped_existing", "failed", "dry_run"]
    slack: Literal["sent", "skipped_existing", "failed", "dry_run"]
    error: str | None = None  # Korean message a person can act on
