"""Answer a regulation question from the index (instruction v2, sections 7 and 8.2).

Default path (explicit pipeline):
1. Conditions the user stated (target, length of service, time) are kept in a
   ConversationState for the last five questions. Earlier answers are never
   used as conditions or evidence.
2. A question about a period the index does not hold ("작년 기준", "개정 전",
   an earlier year) is answered `source_unavailable` without a model call.
3. Retrieval (retrieve.py). A follow-up ("그럼 반차는요?") is searched together
   with the previous question. When the gate closes, the answer is
   `escalation_required` without a model call.
4. Generation with `generate_structured` into ModelAnswer: the model picks
   evidence ids only; quotes and locations are drawn from the stored text.
5. validate.py. On a failed check the model gets the failures and one more
   try; if that fails too, the reply is `escalation_required` with the
   retrieved source cards only.
6. Every `escalation_required` reply leaves a draft inquiry in the pending
   file. Nothing is sent anywhere.

Tool mode: the same retrieval exposed as read-only tools in a function
calling loop that can end only through submit_answer, request_clarification
or escalate. Its answer goes through the same checks; no regeneration.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..llm.client import LLMUnavailable, ToolCallRecord, declare_function
from .chunker import flow_text
from .escalation import save_escalation
from .index import LoadedIndex
from .models import ModelAnswer, RegAnswer, RegChunk
from .parse_law import IMAGE_MARKER, article_label
from .retrieve import DEFAULT_MODE, Hit, RetrievalResult, Retriever
from .usage import BudgetExceeded
from .validate import Validation, validate_answer

PROMPT_DIR = Path(__file__).parent / "prompts"
MAX_TURNS_KEPT = 5
DISCLAIMER = "안내용 답변이며, 개인별 적용은 인사담당자에게 확인하세요."
NOT_CHECKED = "조건과 적용 범위의 의미 해석이 맞는지는 자동으로 검증하지 않습니다."

CONDITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "대상",
        re.compile(
            r"시간선택제\w*|한시임기제\w*|임기제\w*|계약직\w*|공무직\w*|시보\w*|신규\s?임용\w*|신입\w*"
            r"|교원\w*|교사\w*|경찰\w*|소방\w*|군인\w*|고위공직자\w*|임신\s?중\w*|임산부\w*"
            r"|(?:여성|남성)\s?공무원\w*|육아\s?휴직\w*"
        ),
    ),
    (
        "재직기간",
        re.compile(
            r"(?:입사|임용|재직|근무)\s*\d+\s*(?:년|개월)\w*|\d+\s*(?:년|개월)\s*(?:차|째|근무|재직|이상|미만)\w*"
        ),
    ),
    ("시점", re.compile(r"(?:19|20)\d{2}\s*년\w*|작년\w*|지난해\w*|재작년\w*|개정\s*전\w*|예전\w*|과거\w*|당시\w*")),
)
PAST = re.compile(r"작년|지난해|재작년|개정\s*전|예전|과거|당시")
YEAR = re.compile(r"((?:19|20)\d{2})\s*년")
FOLLOW_UP = re.compile(r"^(?:그럼|그러면|그건|그거|그 경우|만약|또)\b|(?:은요|는요|이면요|라면요|면요)\s*\??$")


@dataclass
class Condition:
    type: str
    text: str
    turn: int


@dataclass
class ConversationState:
    questions: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    turn: int = 0

    def add(self, question: str) -> None:
        self.turn += 1
        self.questions = (self.questions + [question])[-MAX_TURNS_KEPT:]
        for kind, pattern in CONDITION_PATTERNS:
            for match in pattern.finditer(question):
                text = match.group(0).strip()
                same = next((c for c in self.conditions if c.type == kind and c.text == text), None)
                if same:
                    same.turn = self.turn  # said again: keep it as fresh as the latest mention
                else:
                    self.conditions.append(Condition(kind, text, self.turn))
        oldest = self.turn - MAX_TURNS_KEPT + 1
        self.conditions = [c for c in self.conditions if c.turn >= oldest]

    def clear(self) -> None:
        self.questions, self.conditions = [], []

    def describe(self) -> str:
        return " / ".join(f"{c.type}: {c.text}" for c in self.conditions) or "(없음)"


def is_follow_up(question: str) -> bool:
    return len(question.strip()) <= 30 and bool(FOLLOW_UP.search(question.strip()))


@dataclass
class AnswerResult:
    answer: RegAnswer
    retrieval: RetrievalResult | None
    cards: list[Hit]  # evidence to show: cited chunks first, then the rest retrieved
    cited_ids: list[str]
    warnings: list[str] = field(default_factory=list)
    escalation_id: str | None = None
    seconds: float = 0.0
    generate_requests: int = 0
    attempts: list[dict] = field(default_factory=list)  # raw model outputs, for rescoring
    tool_calls: list[dict] = field(default_factory=list)


class QAService:
    def __init__(
        self,
        index: LoadedIndex,
        retriever: Retriever,
        llm: Any,
        *,
        escalations: Path,
        counter: Any | None = None,
        primary_model: str | None = None,
        top_k: int = 6,
        mode: str = DEFAULT_MODE,
        max_agent_steps: int = 4,
        clock: Callable[[], float] = time.monotonic,
        tool_log: Path | None = None,
    ) -> None:
        self.index = index
        self.retriever = retriever
        self.llm = llm
        self.counter = counter  # CountingGenai: its `purpose` labels the calls
        self.escalations = escalations
        self.primary_model = primary_model
        self.top_k = top_k
        self.mode = mode
        self.max_agent_steps = max_agent_steps
        self.clock = clock
        self.tool_log = tool_log  # tool-mode transcripts (names, arguments, returned ids; no keys)
        self.system = (PROMPT_DIR / "qa_system.md").read_text(encoding="utf-8")
        self.doc_titles = {d.doc_id: d.doc_title for d in index.docs.values()}

    # --- explicit pipeline ----------------------------------------------------------------

    def answer(self, question: str, state: ConversationState, *, purpose: str = "app_question") -> AnswerResult:
        start = self.clock()
        follow_up = is_follow_up(question) and bool(state.questions)
        context = state.questions[-1:] if follow_up else []
        state.add(question)
        retrieval = self.retriever.search(question, top_k=self.top_k, mode=self.mode, context=context)
        base = {"retrieval_mode": retrieval.mode, "degraded_retrieval": retrieval.degraded}

        unavailable = self._period_unavailable(question, retrieval)
        if unavailable:
            answer = RegAnswer(
                status="source_unavailable",
                escalation_reason=unavailable,
                basis_version=self._basis(retrieval.hits),
                **base,
            )
            return self._finish(answer, retrieval, [], start, question, state)
        if retrieval.gated:
            answer = RegAnswer(
                status="escalation_required",
                escalation_reason=retrieval.gate_reason,
                basis_version=self._basis(retrieval.hits),
                **base,
            )
            return self._finish(answer, retrieval, [], start, question, state, escalate=True)

        retrieved = {hit.chunk.chunk_id: hit.chunk for hit in retrieval.hits}
        prompt = self._prompt(question, state, retrieval, context)
        result = AnswerResult(RegAnswer(status="escalation_required"), retrieval, [], [])
        model_answer, model, validation = None, None, None
        for attempt, label in enumerate((purpose, "regenerate")):
            text = prompt if attempt == 0 else prompt + self._feedback(validation)
            try:
                model_answer, model = self._generate(text, label)
            except (LLMUnavailable, BudgetExceeded) as exc:
                result.attempts.append({"attempt": attempt + 1, "error": type(exc).__name__, "detail": str(exc)[:200]})
                if attempt == 0:
                    model_answer = None
                break
            result.generate_requests += 1
            conditions = [(c.type, c.text) for c in state.conditions]
            validation = validate_answer(model_answer, retrieved, self.doc_titles, conditions)
            result.attempts.append(
                {
                    "attempt": attempt + 1,
                    "model": model,
                    "output": model_answer.model_dump(),
                    "checks": validation.checks,
                    "failures": validation.failures,
                }
            )
            if validation.ok:
                break
        if model_answer is None:
            reason = "모델을 쓸 수 없어(장애·예산) 관련 원문만 보여 드립니다."
            answer = RegAnswer(
                status="escalation_required",
                escalation_reason=reason,
                basis_version=self._basis(retrieval.hits),
                fallback_used=True,
                **base,
            )
            return self._finish(answer, retrieval, [], start, question, state, escalate=True, result=result)
        if not validation.ok:
            answer = RegAnswer(
                status="escalation_required",
                escalation_reason="AI 답변이 근거 검증을 두 번 통과하지 못해 관련 원문만 보여 드립니다: "
                + "; ".join(validation.failures[:3]),
                basis_version=self._basis(retrieval.hits),
                model_used=model,
                fallback_used=self._is_fallback(model),
                checks=validation.checks,
                **base,
            )
            return self._finish(answer, retrieval, [], start, question, state, escalate=True, result=result)
        answer = self._to_answer(model_answer, model, validation, retrieval, base)
        cited = list(dict.fromkeys(eid for item in answer.points + answer.conditions for eid in item.evidence_ids))
        result.warnings = validation.warnings
        return self._finish(
            answer,
            retrieval,
            cited,
            start,
            question,
            state,
            escalate=answer.status == "escalation_required",
            result=result,
        )

    # --- tool mode (function calling, read-only tools) ----------------------------------------

    def answer_with_tools(self, question: str, state: ConversationState, *, purpose: str = "tool_mode") -> AnswerResult:
        start = self.clock()
        state.add(question)
        tools = regulation_tools()
        found: dict[str, RegChunk] = {}

        def respond(call: ToolCallRecord) -> dict[str, Any]:
            return self._run_tool(call, found)

        def is_done(call: ToolCallRecord) -> bool:
            return call.name in TERMINAL_TOOLS

        prompt = (
            f"질문: {question}\n정리된 조건: {state.describe()}\n"
            "search_regulations, get_article, get_chunk로 원문을 찾은 뒤 submit_answer, "
            "request_clarification, escalate 중 하나로 끝내세요."
        )
        result = AnswerResult(RegAnswer(status="escalation_required"), None, [], [])
        if self.counter is not None:
            self.counter.purpose = purpose
        try:
            loop, model = self.llm.run_tool_loop(
                prompt,
                [spec.declaration for spec in tools],
                respond=respond,
                is_done=is_done,
                max_turns=self.max_agent_steps,
                system=self.system,
                require_call=True,
            )
        except (LLMUnavailable, BudgetExceeded) as exc:
            answer = RegAnswer(
                status="escalation_required",
                escalation_reason=f"도구 모드를 쓸 수 없습니다: {exc}",
                retrieval_mode="tool",
                fallback_used=True,
            )
            return self._finish(answer, None, [], start, question, state, escalate=True, result=result)
        result.tool_calls = [{"turn": c.turn, "name": c.name, "args": c.args} for c in loop.calls]
        result.generate_requests = loop.turns
        found = {}  # what the tools returned in the final conversation only
        transcript = []
        for call in loop.calls:
            returned = []
            if call.name not in TERMINAL_TOOLS:
                before = set(found)
                self._run_tool(call, found)
                returned = [cid for cid in found if cid not in before]
            transcript.append({"turn": call.turn, "name": call.name, "args": call.args, "returned_ids": returned})
        self._log_tools(question, model, loop.stop_reason, transcript)
        final = next((c for c in reversed(loop.calls) if c.name in TERMINAL_TOOLS), None)
        base = {"retrieval_mode": "tool", "model_used": model, "fallback_used": self._is_fallback(model)}
        if final is None:
            answer = RegAnswer(
                status="escalation_required",
                escalation_reason=f"최대 단계({self.max_agent_steps})를 넘었습니다.",
                **base,
            )
            return self._finish(answer, None, [], start, question, state, escalate=True, result=result)
        model_answer = _terminal_answer(final)
        validation = validate_answer(model_answer, found, self.doc_titles, [(c.type, c.text) for c in state.conditions])
        hits = [Hit(chunk, "tool") for chunk in found.values()]
        retrieval = RetrievalResult(query=question, mode="tool", hits=hits)
        if not validation.ok:
            answer = RegAnswer(
                status="escalation_required",
                checks=validation.checks,
                escalation_reason="도구 모드 답변이 근거 검증을 통과하지 못했습니다: "
                + "; ".join(validation.failures[:3]),
                **base,
            )
            return self._finish(answer, retrieval, [], start, question, state, escalate=True, result=result)
        answer = self._to_answer(model_answer, model, validation, retrieval, base)
        cited = list(dict.fromkeys(eid for item in answer.points + answer.conditions for eid in item.evidence_ids))
        result.warnings = validation.warnings
        return self._finish(
            answer,
            retrieval,
            cited,
            start,
            question,
            state,
            escalate=answer.status == "escalation_required",
            result=result,
        )

    def _run_tool(self, call: ToolCallRecord, found: dict[str, RegChunk]) -> dict[str, Any]:
        args = call.args
        if call.name == "search_regulations":
            hits = self.retriever.search(
                str(args.get("query", "")), top_k=min(int(args.get("top_k", 5) or 5), 8), mode=self.mode
            ).hits
            chunks = [h.chunk for h in hits]
        elif call.name == "get_article":
            chunks = self.retriever.article(str(args.get("doc_title", "")), str(args.get("article_no", "")))
        elif call.name == "get_chunk":
            chunk = self.index.chunk(str(args.get("chunk_id", "")))
            chunks = [chunk] if chunk else []
        else:
            return {"ok": True}
        for chunk in chunks:
            found[chunk.chunk_id] = chunk
        return {"results": [self._tool_view(c) for c in chunks]}

    def _log_tools(self, question: str, model: str, stop: str, transcript: list[dict]) -> None:
        if self.tool_log is None:
            return
        self.tool_log.parent.mkdir(parents=True, exist_ok=True)
        record = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "question": question,
                  "model": model, "stop_reason": stop, "calls": transcript}
        with self.tool_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _tool_view(self, chunk: RegChunk) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "doc_title": self.doc_titles.get(chunk.doc_id),
            "article": self.label(chunk),
            "text": model_text(chunk, self.index),
        }

    # --- helpers --------------------------------------------------------------------------

    def label(self, chunk: RegChunk) -> str:
        if not chunk.article_no:
            return chunk.heading or ""
        text = article_label(chunk.article_no)
        text += f"({chunk.article_title})" if chunk.article_title else ""
        text += f" 제{chunk.paragraph_no}항" if chunk.paragraph_no else ""
        return text + (" 표(전사본)" if chunk.kind == "table" else "")

    def _generate(self, prompt: str, purpose: str) -> tuple[ModelAnswer, str]:
        if self.counter is not None:
            self.counter.purpose = purpose
        return self.llm.generate_structured(prompt, ModelAnswer, system=self.system)

    def _prompt(self, question: str, state: ConversationState, retrieval: RetrievalResult, context: list[str]) -> str:
        lines = [f"질문: {question}"]
        if context:
            lines.append("앞선 질문(같은 대화, 무엇을 묻는지 참고만): " + " / ".join(context))
        lines.append(f"정리된 조건(사용자가 말한 것만): {state.describe()}")
        lines.append(f"기준: 색인된 현행 버전 — {self._basis(retrieval.hits)}")
        lines.append("근거:")
        for hit in retrieval.hits:
            tag = " (참조 조문: 앞 근거가 가리키는 조문)" if hit.via == "ref" else ""
            lines.append(f"[{hit.chunk.chunk_id}] {self.doc_titles.get(hit.chunk.doc_id)} {self.label(hit.chunk)}{tag}")
            lines.append(model_text(hit.chunk, self.index))
        return "\n".join(lines)

    @staticmethod
    def _feedback(validation: Validation | None) -> str:
        if validation is None:
            return ""
        return (
            "\n\n앞 답변이 원문 검증에서 다음 이유로 거절됐습니다. 근거 원문에 있는 그대로만 다시 답하세요:\n- "
            + "\n- ".join(validation.failures)
        )

    def _to_answer(
        self,
        model_answer: ModelAnswer,
        model: str | None,
        validation: Validation,
        retrieval: RetrievalResult,
        base: dict,
    ) -> RegAnswer:
        cited = [
            retrieval_chunk
            for retrieval_chunk in (h.chunk for h in retrieval.hits)
            if any(
                retrieval_chunk.chunk_id in item.evidence_ids for item in model_answer.points + model_answer.conditions
            )
        ]
        data = model_answer.model_dump()
        if model_answer.status != "answered":
            # Only an answered reply is checked point by point, so no headline or points are shown otherwise.
            data.update(one_line=None, points=[], conditions=[])
        data.update(base)
        data.update(
            model_used=model,
            fallback_used=self._is_fallback(model),
            checks=validation.checks,
            basis_version=self._basis([Hit(c, "cited") for c in cited] or retrieval.hits),
        )
        return RegAnswer.model_validate(data)

    def _basis(self, hits: list[Hit]) -> str:
        labels = []
        for doc_id in dict.fromkeys(h.chunk.doc_id for h in hits):
            doc = self.index.docs.get(doc_id)
            if doc is None:
                continue
            if doc.doc_type == "annex":
                labels.append(f"{doc.doc_title} {doc.version_label or '별표'} (시행일 미확인)")
                continue
            when = f"시행 {_dot(doc.effective_date)}" if doc.effective_date else "시행일 미확인"
            label = f"{doc.doc_title} {when}" + (f" ({doc.version_label})" if doc.version_label else "")
            if "manual_transcription" in doc.unverified:
                label += " · 표 전사본 검수 대기"
            labels.append(label)
        return "; ".join(dict.fromkeys(labels)) or "미확인"

    def _period_unavailable(self, question: str, retrieval: RetrievalResult) -> str | None:
        docs = [
            self.index.docs[d] for d in dict.fromkeys(h.chunk.doc_id for h in retrieval.hits) if d in self.index.docs
        ]
        dated = [d.effective_date for d in docs if d.effective_date]
        if PAST.search(question):
            return "색인에는 현행 버전만 있어 과거 시점의 규정으로는 답할 수 없습니다."
        years = [int(y) for y in YEAR.findall(question)]
        if years and dated and min(years) < min(d.year for d in dated):
            return f"{min(years)}년 기준 규정은 색인에 없습니다. 색인된 버전: " + ", ".join(
                f"{d.doc_title} 시행 {_dot(d.effective_date)}" for d in docs if d.effective_date
            )
        return None

    def _is_fallback(self, model: str | None) -> bool:
        return bool(model and self.primary_model and model != self.primary_model)

    def _finish(
        self,
        answer: RegAnswer,
        retrieval: RetrievalResult | None,
        cited: list[str],
        start: float,
        question: str,
        state: ConversationState,
        *,
        escalate: bool = False,
        result: AnswerResult | None = None,
    ) -> AnswerResult:
        result = result or AnswerResult(answer, retrieval, [], [])
        result.answer, result.retrieval, result.cited_ids = answer, retrieval, cited
        hits = retrieval.hits if retrieval else []
        result.cards = [h for h in hits if h.chunk.chunk_id in cited] + [
            h for h in hits if h.chunk.chunk_id not in cited
        ]
        if escalate:
            result.escalation_id = save_escalation(
                self.escalations,
                question=question,
                conditions=state.describe(),
                candidates=[h.chunk.chunk_id for h in hits],
                reason=answer.escalation_reason or "",
            )
        result.seconds = round(self.clock() - start, 2)
        return result


def model_text(chunk: RegChunk, index: LoadedIndex | None = None) -> str:
    """Source text for the model: amendment notes dropped. The image-table marker is kept,
    or points at the transcription when the article has one."""
    text = flow_text(chunk.text, keep_marker=True)
    tables = [ref for ref in chunk.refs if index and (t := index.chunk(ref)) and t.kind == "table"]
    if tables and IMAGE_MARKER in text:
        text = text.replace(IMAGE_MARKER, f"[표(이미지) — 옮겨 적은 표 전사본: {', '.join(tables)}]")
    if chunk.kind == "table":
        text = "[표 전사본 — 원문 PDF의 이미지 표를 옮겨 적음(사람 검수 전일 수 있음)] " + text
    return text


def _dot(day: date | None) -> str:
    return f"{day.year}. {day.month}. {day.day}." if day else "미확인"


# --- read-only tools ---------------------------------------------------------------------------


class SearchArgs(BaseModel):
    query: str
    top_k: int = 5


class ArticleArgs(BaseModel):
    doc_title: str
    article_no: str  # "15" or "15의2"


class ChunkArgs(BaseModel):
    chunk_id: str


class ClarifyArgs(BaseModel):
    clarification_question: str
    missing_conditions: list[str] = []


class EscalateArgs(BaseModel):
    escalation_reason: str
    candidate_ids: list[str] = []


@dataclass(frozen=True)
class ToolSpec:
    name: str
    side_effect: bool
    declaration: Any


TERMINAL_TOOLS = ("submit_answer", "request_clarification", "escalate")


def regulation_tools() -> list[ToolSpec]:
    """Read-only regulation tools plus the three ways to finish. No Calendar, Slack, file or SQL tool."""
    specs = [
        ToolSpec(
            "search_regulations",
            False,
            declare_function("search_regulations", "규정 색인에서 질문과 관련된 조문을 찾는다(읽기 전용).", SearchArgs),
        ),
        ToolSpec(
            "get_article",
            False,
            declare_function("get_article", "법령명과 조문 번호로 조문 원문을 가져온다(읽기 전용).", ArticleArgs),
        ),
        ToolSpec("get_chunk", False, declare_function("get_chunk", "근거 ID로 원문을 가져온다(읽기 전용).", ChunkArgs)),
        ToolSpec(
            "submit_answer",
            False,
            declare_function(
                "submit_answer", "찾은 원문에 근거한 최종 답변을 낸다. 근거는 ID로만 고른다.", ModelAnswer
            ),
        ),
        ToolSpec(
            "request_clarification",
            False,
            declare_function("request_clarification", "답에 꼭 필요한 조건이 빠졌을 때 되묻는다.", ClarifyArgs),
        ),
        ToolSpec(
            "escalate",
            False,
            declare_function("escalate", "근거가 부족해 담당자 확인이 필요할 때 끝낸다.", EscalateArgs),
        ),
    ]
    assert not any(spec.side_effect for spec in specs)
    return specs


def _terminal_answer(call: ToolCallRecord) -> ModelAnswer:
    args = dict(call.args)
    if call.name == "request_clarification":
        return ModelAnswer(
            status="needs_clarification",
            clarification_question=args.get("clarification_question"),
            missing_conditions=list(args.get("missing_conditions") or []),
        )
    if call.name == "escalate":
        return ModelAnswer(status="escalation_required", escalation_reason=args.get("escalation_reason"))
    try:
        return ModelAnswer.model_validate(args)
    except Exception:
        return ModelAnswer(status="escalation_required", escalation_reason="submit_answer 인자를 해석하지 못했습니다.")


def dump_attempts(result: AnswerResult) -> str:
    return json.dumps(result.attempts, ensure_ascii=False)
