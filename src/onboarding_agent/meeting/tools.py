"""Function-calling tools for meeting extraction.

The tools only record content. Nothing here touches Calendar or Slack: those
run in the executor after the user approves. Tool arguments are untrusted
data and are validated against the LLM output schemas.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from ..llm.client import ToolCallRecord, ToolLoopResult, declare_function
from .models import LLMActionItem, LLMExtraction, LLMFinish, LLMOverview, ToolCallSummary
from .roster import compact

OVERVIEW = "record_meeting_overview"
PROPOSE = "propose_action_item"
FINISH = "finish_extraction"
TOOL_NAMES = (OVERVIEW, PROPOSE, FINISH)
MAX_TURNS = 4

_ARG_MODELS: dict[str, type[BaseModel]] = {
    OVERVIEW: LLMOverview,
    PROPOSE: LLMActionItem,
    FINISH: LLMFinish,
}

TOOLS = [
    declare_function(
        OVERVIEW,
        "회의 제목 제안, 요약 3~5개, 결정사항, 미결 사항을 기록한다. 정확히 1번 호출한다.",
        LLMOverview,
    ),
    declare_function(
        PROPOSE,
        "액션 아이템 1건을 제안한다. 항목마다 1번씩 호출하고, 항목이 없으면 호출하지 않는다. "
        "기록만 하며 일정 등록이나 알림 발송은 하지 않는다.",
        LLMActionItem,
    ),
    declare_function(
        FINISH,
        "모든 기록과 제안을 마친 뒤 마지막에 1번 호출한다. item_count에 제안한 액션 아이템 수를 넣는다.",
        LLMFinish,
    ),
]


def _error_reason(exc: ValidationError) -> str:
    missing = [".".join(str(p) for p in e["loc"]) for e in exc.errors() if e["type"] == "missing"]
    invalid = [
        ".".join(str(p) for p in e["loc"]) for e in exc.errors() if e["type"] != "missing"
    ]
    parts = []
    if missing:
        parts.append("필수 인자 누락: " + ", ".join(missing))
    if invalid:
        parts.append("형식 오류: " + ", ".join(invalid))
    return " / ".join(parts) or "인자 형식 오류"


def parse_call(record: ToolCallRecord) -> tuple[BaseModel | None, str | None]:
    """Validate one call's arguments. Returns (parsed args, rejection reason)."""
    model = _ARG_MODELS.get(record.name)
    if model is None:
        return None, f"알 수 없는 도구입니다: {record.name}"
    try:
        parsed = model.model_validate(record.args)
    except ValidationError as exc:
        return None, _error_reason(exc)
    if isinstance(parsed, LLMActionItem) and not (
        parsed.task.strip() and parsed.evidence_quote.strip()
    ):
        return None, "task와 evidence_quote는 비워 둘 수 없습니다."
    if isinstance(parsed, LLMOverview) and any(not d.text.strip() for d in parsed.decisions):
        return None, "결정사항 text는 비워 둘 수 없습니다."
    return parsed, None


def respond(record: ToolCallRecord) -> dict[str, Any]:
    """Tool result sent back to the model. Pure: no state between calls."""
    _, reason = parse_call(record)
    if reason:
        return {"result": "rejected", "reason": reason}
    if record.name == FINISH:
        return {"result": "finished"}
    return {"result": "recorded"}


def is_finish(record: ToolCallRecord) -> bool:
    return record.name == FINISH


def _dedupe_key(item: LLMActionItem) -> tuple[str, str, str]:
    return (compact(item.task), compact(item.owner_name or ""), compact(item.due_text or ""))


def _merge(kept: LLMActionItem, extra: LLMActionItem) -> LLMActionItem:
    update: dict[str, Any] = {
        name: getattr(extra, name)
        for name in ("owner_name", "due_text", "due_date_guess", "due_time_guess")
        if getattr(kept, name) is None and getattr(extra, name) is not None
    }
    co_owners = list(dict.fromkeys([*kept.co_owners, *extra.co_owners]))
    if co_owners != kept.co_owners:
        update["co_owners"] = co_owners
    return kept.model_copy(update=update) if update else kept


@dataclass
class CollectedCalls:
    overview: LLMOverview | None = None
    items: list[LLMActionItem] = field(default_factory=list)
    finished: bool = False
    finish_count: int | None = None
    valid_calls: int = 0
    rejected: int = 0
    merged_duplicates: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def fallback_reasons(self) -> list[str]:
        """Why the function-calling result cannot be used as is (empty when usable)."""
        reasons = []
        if self.valid_calls == 0:
            reasons.append("유효한 도구 호출이 하나도 없습니다")
        if self.overview is None:
            reasons.append(f"{OVERVIEW} 호출이 없습니다")
        if not self.finished:
            reasons.append(f"{MAX_TURNS}턴 안에 {FINISH} 호출이 오지 않았습니다")
        return reasons

    def to_extraction(self) -> LLMExtraction:
        overview = self.overview or LLMOverview(summary=[])
        return LLMExtraction(
            title_suggestion=overview.title_suggestion,
            summary=overview.summary,
            decisions=overview.decisions,
            action_items=self.items,
            open_issues=overview.open_issues,
        )

    def summary(self, loop: ToolLoopResult) -> ToolCallSummary:
        return ToolCallSummary(
            turns=loop.turns,
            calls=dict(self.counts),
            rejected=self.rejected,
            merged_duplicates=self.merged_duplicates,
            finished=self.finished,
        )


def collect(calls: Sequence[ToolCallRecord]) -> CollectedCalls:
    """Fold the transcript into an extraction, dropping invalid calls with warnings."""
    out = CollectedCalls()
    keys: dict[tuple[str, str, str], int] = {}
    for record in calls:
        out.counts[record.name] += 1
        parsed, reason = parse_call(record)
        if reason:
            out.rejected += 1
            out.warnings.append(f"{record.name} 호출 1건을 버렸습니다 ({reason}).")
            continue
        out.valid_calls += 1
        if isinstance(parsed, LLMOverview):
            if out.overview is None:
                out.overview = parsed
            else:
                out.warnings.append(f"{OVERVIEW}가 여러 번 호출되어 첫 번째 기록만 썼습니다.")
        elif isinstance(parsed, LLMActionItem):
            key = _dedupe_key(parsed)
            if key in keys:
                index = keys[key]
                out.items[index] = _merge(out.items[index], parsed)
                out.merged_duplicates += 1
            else:
                keys[key] = len(out.items)
                out.items.append(parsed)
        elif isinstance(parsed, LLMFinish):
            if out.finished:
                out.warnings.append(f"{FINISH}가 여러 번 호출되어 첫 번째만 썼습니다.")
            else:
                out.finished = True
                out.finish_count = parsed.item_count

    if out.merged_duplicates:
        out.warnings.append(f"같은 내용의 제안 {out.merged_duplicates}건을 하나로 합쳤습니다.")
    if out.finished and out.finish_count != len(out.items):
        out.warnings.append(
            f"{FINISH}에 적힌 항목 수({out.finish_count})와 실제 제안 수({len(out.items)})가 다릅니다."
        )
    return out
