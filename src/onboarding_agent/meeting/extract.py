"""Meeting extraction: function calling first, then structured output, then rules.

Paths (recorded in ExtractionResult.extraction_path):
1. function_calling — the agent records the overview, proposes action items
   one call at a time and finishes; up to MAX_TURNS turns.
2. structured — one generate_structured(LLMExtraction) call, used once when
   the tool transcript has no overview, no valid call, or no finish.
3. rule_based — when the LLM is unavailable or FORCE_FALLBACK is on.
Every path ends in the same deterministic validation.
"""

from __future__ import annotations

import re
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from importlib import resources

from .. import metrics
from ..config import Settings, get_settings
from ..llm.client import LLMClient, LLMUnavailable, get_client
from .dates import weekday_label
from .fallback_rules import extract_with_rules
from .loader import MeetingInputError
from .models import (
    ExtractionPath,
    ExtractionResult,
    LLMExtraction,
    Meeting,
    ToolCallSummary,
    new_id,
    source_hash,
)
from .roster import Roster, RosterError, load_roster
from .tools import MAX_TURNS, TOOL_NAMES, TOOLS, collect, is_finish, respond
from .validate import count_unconfirmed, validate_extraction

PROMPT_FILE = "meeting_extract.md"

# progress(stage, detail): stages are "llm", "structured", "rules", "validate".
Progress = Callable[[str, str], None]


def _no_progress(stage: str, detail: str) -> None:
    del stage, detail


@dataclass(frozen=True)
class PromptTemplate:
    version: str
    sections: dict[str, str]


@lru_cache(maxsize=1)
def load_prompt() -> PromptTemplate:
    text = (resources.files(__package__) / "prompts" / PROMPT_FILE).read_text(encoding="utf-8")
    version = re.search(r"prompt:\s*(\S+\s+v\d+)", text)
    sections: dict[str, str] = {}
    for block in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        name, _, body = block.partition("\n")
        sections[name.strip()] = body.strip()
    return PromptTemplate(version[1] if version else "unknown", sections)


def _escape(text: str) -> str:
    # Keep the minutes from closing the data tag early.
    return text.replace("<회의록>", "＜회의록＞").replace("</회의록>", "＜/회의록＞")


def build_prompts(text: str, title: str, meeting_date: date) -> tuple[str, str, str]:
    """Return (system prompt for tools, system prompt for JSON, user message)."""
    prompt = load_prompt().sections
    system_tools = "\n\n".join([prompt["system"], prompt["tools"], prompt["example"]])
    system_json = "\n\n".join([prompt["system"], prompt["structured"], prompt["example"]])
    user = (
        prompt["user"]
        .replace("<<MEETING_DATE>>", meeting_date.isoformat())
        .replace("<<WEEKDAY>>", weekday_label(meeting_date) + "요일")
        .replace("<<TITLE>>", _escape(title or "(없음)"))
        .replace("<<MEETING_TEXT>>", _escape(text))
    )
    return system_tools, system_json, user


@dataclass
class _LLMAttempt:
    extraction: LLMExtraction | None = None
    path: ExtractionPath = "rule_based"
    model: str | None = None
    tool_summary: ToolCallSummary | None = None


# Why the model could not be used, in words for the person at the screen (matched on the error text).
_FAILURE_REASONS = (
    (("RESOURCE_EXHAUSTED", " 429", " 402", "quota"), "Gemini 사용 한도를 넘었습니다(할당량·결제 상태를 확인하세요)"),
    (
        ("UNAUTHENTICATED", "PERMISSION_DENIED", " 401", " 403", "API key"),
        "Gemini API 키가 유효하지 않거나 권한이 없습니다",
    ),
    (("NOT_FOUND", " 404"), "지정한 모델 이름을 찾을 수 없습니다(GEMINI_MODEL_* 확인)"),
    (("DEADLINE_EXCEEDED", "Timeout", "timed out"), "모델 응답 시간이 초과됐습니다"),
    (("UNAVAILABLE", "INTERNAL", " 500", " 502", " 503", " 504"), "Gemini 서버가 일시적으로 응답하지 않습니다"),
    (("RemoteProtocolError", "ConnectError", "Connection"), "네트워크 연결 오류가 났습니다"),
)
_last_failure: dict[str, str] = {}  # the latest reason, for "recently failed" skips in the same process


def describe_llm_failure(message: str) -> str | None:
    """A Korean reason for an LLMUnavailable message, or None when the message says it already."""
    for needles, reason in _FAILURE_REASONS:
        if any(needle in message for needle in needles):
            _last_failure["reason"] = reason
            return reason
    if "최근 실패한 모델" in message and "reason" in _last_failure:
        return f"직전 원인: {_last_failure['reason']}"
    return None


def _fallback_warning(lead: str, exc: Exception) -> str:
    reason = describe_llm_failure(str(exc))
    return f"{lead} — {reason}. (자세한 오류: {exc})" if reason else f"{lead} ({exc})"


def _run_llm(
    client: LLMClient,
    text: str,
    title: str,
    meeting_date: date,
    warnings: list[str],
    progress: Progress,
) -> _LLMAttempt:
    """Function calling, then at most one structured call. Never raises LLMUnavailable."""
    attempt = _LLMAttempt()
    system_tools, system_json, user = build_prompts(text, title, meeting_date)
    progress("llm", " → ".join(client.available_models() or client.settings.gemini_models))
    try:
        loop, model = client.run_tool_loop(
            user,
            TOOLS,
            respond=respond,
            is_done=is_finish,
            max_turns=MAX_TURNS,
            system=system_tools,
            allowed_function_names=TOOL_NAMES,
        )
    except LLMUnavailable as exc:
        warnings.append(_fallback_warning("LLM을 사용할 수 없어 규칙 기반으로 추출했습니다", exc))
        return attempt

    collected = collect(loop.calls)
    attempt.tool_summary = collected.summary(loop)
    warnings.extend(collected.warnings)
    reasons = collected.fallback_reasons()
    if not reasons:
        attempt.extraction, attempt.path, attempt.model = (
            collected.to_extraction(), "function_calling", model,
        )
        return attempt

    warnings.append("도구 호출 결과가 불완전해 구조화 출력으로 다시 추출했습니다: " + ", ".join(reasons) + ".")
    progress("structured", ", ".join(reasons))
    try:
        attempt.extraction, attempt.model = client.generate_structured(user, LLMExtraction, system_json)
        attempt.path = "structured"
    except LLMUnavailable as exc:
        warnings.append(_fallback_warning("구조화 출력도 실패해 규칙 기반으로 추출했습니다", exc))
    return attempt


def extract_meeting(
    text: str,
    *,
    title: str,
    meeting_date: date,
    source_filename: str | None = None,
    roster: Roster | None = None,
    force_fallback: bool | None = None,
    client: LLMClient | None = None,
    settings: Settings | None = None,
    log_metrics: bool = True,
    now: datetime | None = None,
    progress: Progress | None = None,
    raw_extractions: list[LLMExtraction] | None = None,
) -> ExtractionResult:
    """Extract, validate and package one meeting. Nothing is saved or sent here.

    `raw_extractions`, when given, receives the extraction before validation
    (evaluation scripts use it to score the same output under other rules).
    """
    settings = settings or get_settings()
    progress = progress or _no_progress
    if len(text) > settings.meeting_max_chars:
        raise MeetingInputError(f"회의록이 너무 깁니다 ({len(text):,}자).")
    warnings: list[str] = []
    if roster is None:
        try:
            roster = load_roster(settings.roster_path)
        except RosterError as exc:
            warnings.append(str(exc))
            roster = Roster()
        if not roster.members:
            warnings.append("명단이 비어 있어 담당자의 Slack ID를 채울 수 없습니다.")

    started = time_module.perf_counter()
    forced = settings.force_fallback if force_fallback is None else force_fallback
    if forced:
        warnings.append("강제 폴백이 켜져 있어 LLM 없이 규칙 기반으로 추출했습니다.")
        attempt = _LLMAttempt()
    else:
        attempt = _run_llm(client or get_client(), text, title, meeting_date, warnings, progress)
    path, model_used, tool_summary = attempt.path, attempt.model, attempt.tool_summary
    if attempt.extraction is None:
        progress("rules", "강제 폴백" if forced else "LLM 사용 불가")
    extraction = attempt.extraction or extract_with_rules(text, meeting_date=meeting_date, roster=roster)
    if raw_extractions is not None:
        raw_extractions.append(extraction)
    progress("validate", "")

    meeting_id = new_id()
    validated = validate_extraction(
        extraction,
        meeting_text=text,
        meeting_date=meeting_date,
        meeting_id=meeting_id,
        roster=roster,
        from_rules=path == "rule_based",
    )
    warnings.extend(validated.warnings)
    meeting = Meeting(
        meeting_id=meeting_id,
        title=title.strip() or extraction.title_suggestion or "제목 없는 회의",
        meeting_date=meeting_date,
        source_filename=source_filename,
        source_hash=source_hash(text, meeting_date),
        summary=validated.summary,
        decisions=validated.decisions,
        open_issues=validated.open_issues,
        created_at=now or datetime.now(settings.tz),
    )
    result = ExtractionResult(
        meeting=meeting,
        action_items=validated.items,
        extraction_path=path,
        model_used=model_used,
        fallback_used=path != "function_calling",
        tool_calls=tool_summary,
        injection_sentences=validated.injection_sentences,
        injection_blocked=validated.dropped_by_injection,
        warnings=warnings,
    )
    if log_metrics:
        metrics.log_event(
            metrics.MEETING_EXTRACTED,
            input_chars=len(text),
            items=len(result.action_items),
            unconfirmed=count_unconfirmed(result.action_items),
            needs_review=sum(1 for i in result.action_items if i.needs_review),
            model=model_used,
            path=path,
            fallback=result.fallback_used,
            latency_ms=round((time_module.perf_counter() - started) * 1000),
            tool_turns=tool_summary.turns if tool_summary else 0,
            tool_calls=tool_summary.calls if tool_summary else {},
            prompt_version=load_prompt().version,
        )
    return result
