"""Data contracts for regulation Q&A (instruction v2, sections 5 and 7.4).

Unknown values stay None and are listed in `unverified`; nothing is filled in
from the collection date or guessed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

AnswerStatus = Literal["answered", "needs_clarification", "escalation_required", "source_unavailable"]
CheckResult = Literal["passed", "failed", "warning", "not_applicable"]


class SourceDoc(BaseModel):
    doc_id: str  # deterministic; includes the source's version id when there is one
    doc_title: str
    doc_type: Literal["law", "admin_rule", "guide", "annex"]
    version_label: str | None = None  # as printed by the source, e.g. "대통령령 제36728호"
    effective_date: date | None = None  # only when the source states it
    promulgation_date: date | None = None
    applies_to: str | None = None
    source_kind: Literal["law_api", "local_file", "manual_download"]
    source_uri: str | None = None
    source_sha256: str
    collected_at: datetime  # when the file was read
    is_fictional: bool = False
    external_send_allowed: bool
    unverified: list[str] = []


class RegChunk(BaseModel):
    chunk_id: str  # "{doc_id}:a15-2" or "{doc_id}:a15-2:p1" when an article is split
    doc_id: str
    article_no: str | None  # "15", "15의2"
    article_title: str | None
    paragraph_no: str | None
    section_path: str | None
    heading: str | None = None  # section title for guides without articles
    text: str  # source text as extracted (line breaks kept)
    search_text: str  # whitespace-normalised text without amendment notes
    embed_text: str  # header + search_text
    location: dict  # {"pdf_page", "pdf_page_end", "printed_page", "printed_page_end"} or {"lines": [s, e]}
    refs: list[str] = []  # resolved chunk ids, or the source wording when unresolved
    has_proviso: bool


class AnswerPoint(BaseModel):
    text: str
    # One or more. Not enforced by the schema, so a missing id is reported by
    # validate.py (근거 연결) instead of making the model call fail and retry.
    evidence_ids: list[str] = []


class ModelAnswer(BaseModel):
    """What the model fills in. Quotes and locations come from the program."""

    status: Literal["answered", "needs_clarification", "escalation_required"]
    one_line: str | None = None
    points: list[AnswerPoint] = []
    conditions: list[AnswerPoint] = []  # provisos, exceptions and conditions of application
    clarification_question: str | None = None
    missing_conditions: list[str] = []
    escalation_reason: str | None = None


class RegAnswer(BaseModel):
    status: AnswerStatus
    one_line: str | None = None
    points: list[AnswerPoint] = []
    conditions: list[AnswerPoint] = []
    clarification_question: str | None = None
    missing_conditions: list[str] = []
    escalation_reason: str | None = None
    # filled in by the program
    basis_version: str | None = None
    retrieval_mode: str = "none"
    model_used: str | None = None
    fallback_used: bool = False
    degraded_retrieval: bool = False
    checks: dict[str, CheckResult] = {}
