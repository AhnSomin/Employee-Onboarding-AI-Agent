"""Meeting feature models (spec section 7).

LLM output models carry content only. Domain models add IDs, status and
validation results, which code fills in, never the LLM.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, Field, model_validator

FieldStatus = Literal["confirmed", "unconfirmed"]
ItemStatus = Literal["draft", "approved", "done", "cancelled"]
ReminderStage = Literal["D-1", "D-day", "overdue"]
ExtractionPath = Literal["function_calling", "structured", "rule_based"]


def new_id() -> str:
    return str(uuid.uuid4())


# --- LLM output only ---
# Field descriptions are shown to the model (tool parameters and JSON schema).


class LLMDecision(BaseModel):
    text: str = Field(description="무엇을 어떻게 하기로 정했는지 한 문장")
    evidence_quote: str = Field(description="근거가 되는 회의록 원문을 그대로 복사한 1~2문장")


class LLMActionItem(BaseModel):
    task: str = Field(description='"~하기"로 끝나는 한 문장의 할 일')
    owner_name: str | None = Field(
        default=None,
        description="회의록에 명시적으로 지정된 담당자의 이름이나 호칭. 지정이 없거나 개인이 아니면 null",
    )
    co_owners: list[str] = Field(default=[], description="명시된 공동 담당자. 없으면 빈 배열")
    due_text: str | None = Field(
        default=None, description="회의록에 적힌 기한 표현 그대로. 기한이 없으면 null"
    )
    due_date_guess: str | None = Field(
        default=None, description="기준일로 계산한 기한 날짜(YYYY-MM-DD). 참고용이며 불확실하면 null"
    )
    due_time_guess: str | None = Field(
        default=None, description="기한 시각(HH:MM). 회의록에 시각이 없으면 null"
    )
    evidence_quote: str = Field(description="근거가 되는 회의록 원문을 그대로 복사한 1~2문장")


class LLMExtraction(BaseModel):
    title_suggestion: str | None = Field(default=None, description="회의 제목 제안")
    summary: list[str] = Field(description="회의 요약 3~5개. 회의록에 있는 내용만")
    decisions: list[LLMDecision]
    action_items: list[LLMActionItem]
    open_issues: list[str] = Field(default=[], description="결론이 나지 않은 미결 사항")  # P1


class LLMOverview(BaseModel):
    """Arguments of the record_meeting_overview tool."""

    title_suggestion: str | None = Field(default=None, description="회의 제목 제안")
    summary: list[str] = Field(description="회의 요약 3~5개. 회의록에 있는 내용만")
    decisions: list[LLMDecision] = Field(default=[], description="결정사항 목록")
    open_issues: list[str] = Field(default=[], description="결론이 나지 않은 미결 사항")


class LLMFinish(BaseModel):
    """Arguments of the finish_extraction tool."""

    item_count: int = Field(ge=0, description="propose_action_item으로 제안한 액션 아이템 수")


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


class ToolCallSummary(BaseModel):
    """What the agent called during the function-calling attempt (shown in the UI)."""

    turns: int = 0
    calls: dict[str, int] = {}  # tool name -> number of calls, invalid ones included
    rejected: int = 0  # calls dropped because the arguments failed validation
    merged_duplicates: int = 0
    finished: bool = False


class ExtractionResult(BaseModel):
    meeting: Meeting
    action_items: list[ActionItem]
    extraction_path: ExtractionPath
    model_used: str | None
    fallback_used: bool  # True whenever the path is not function_calling
    tool_calls: ToolCallSummary | None = None  # None when no function calling was attempted
    warnings: list[str] = []


class ExecutionResult(BaseModel):
    item_id: str
    calendar: Literal["created", "skipped_existing", "failed", "dry_run"]
    slack: Literal["sent", "skipped_existing", "failed", "dry_run"]
    error: str | None = None  # Korean message a person can act on
