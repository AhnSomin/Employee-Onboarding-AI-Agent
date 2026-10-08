"""Deterministic checks on extracted content (spec 8.3).

Everything the LLM (or the rule-based fallback) returns is untrusted data.
This module turns it into domain objects and decides, in code, which owner
and due fields count as confirmed. Reasons are left in Korean review notes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, time
from difflib import SequenceMatcher

from .dates import format_due, resolve_due
from .models import ActionItem, Decision, LLMActionItem, LLMExtraction, new_id
from .owner_rules import OwnerRules, Speech, default_owner_rules, promise_note
from .roster import Roster, compact, is_group_reference, person_name, strip_titles

DUPLICATE_RATIO = 0.85
INJECTION_OVERLAP_CHARS = 12
QUOTE_PART_GAP = 20  # max characters between parts of a multi-line quote (bullets, speaker tags)
MIN_ELIDED_PIECE = 10  # min characters (no spaces) of each piece of a quote shortened with "…"
_ELISION = re.compile(r"\.{3,}|…+|\(\s*중략\s*\)")

# Sentences in the minutes that address the AI reader instead of people.
_INJECTION_PATTERNS = (
    re.compile(
        r"(?:이|본|해당)\s*(?:회의록|문서|메모|글|파일|내용)\S*\s*(?:을|를)?\s*"
        r"(?:읽|보|요약|처리|분석|정리)\S*\s*(?:AI|인공지능|에이전트|모델|어시스턴트|챗봇|봇|LLM)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:이전|앞의|위의|기존|모든)\s*(?:모든\s*)?(?:지시|명령|지침|규칙|프롬프트)\S*\s*"
        r"(?:(?:모두|전부|다)\s*)?무시"
    ),
    re.compile(r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"시스템\s*프롬프트|system\s*prompt", re.IGNORECASE),
    re.compile(
        r"(?:AI|인공지능|에이전트|어시스턴트|챗봇|LLM)\s*(?:은|는|야|에게|님)\s.*"
        r"(?:하라|해라|보내라|하십시오|하세요|할\s*것|실행하라)\s*[.!]?$",
        re.IGNORECASE,
    ),
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


@dataclass
class ValidatedExtraction:
    summary: list[str]
    decisions: list[Decision]
    items: list[ActionItem]
    open_issues: list[str]
    warnings: list[str] = field(default_factory=list)
    injection_sentences: list[str] = field(default_factory=list)
    dropped_by_injection: int = 0


def normalize_space(text: str) -> str:
    return " ".join(text.split())


def _elided_spans(pieces: list[str], speech: Speech) -> Iterator[list[tuple[int, int]]]:
    """Places where every piece appears, in order, inside one utterance or paragraph."""
    text = speech.text
    start = text.find(pieces[0])
    while start != -1:
        low, high = speech.block_bounds(start)
        spans = [(start, start + len(pieces[0]))]
        if spans[0][1] <= high:
            for piece in pieces[1:]:
                found = text.find(piece, spans[-1][1], high)
                if found == -1:
                    break
                spans.append((found, found + len(piece)))
            else:
                yield spans
        start = text.find(pieces[0], start + 1)


def iter_quote_spans(
    quote: str, normalized_text: str, speech: Speech | None = None
) -> Iterator[list[tuple[int, int]]]:
    """Every place the quote appears in the minutes, as spans in `normalized_text`.

    A quote spanning several lines may skip the bullets or speaker tags
    between them, but its sentences must appear in order and close together,
    so unrelated sentences cannot be stitched into one quote.
    A quote shortened with "...", "…" or "(중략)" counts only when every piece
    has at least MIN_ELIDED_PIECE characters (spaces not counted) and all
    pieces appear in order inside one utterance (one paragraph when the
    minutes have no speaker labels). This needs `speech`.
    """
    quote = normalize_space(quote.replace("“", '"').replace("”", '"'))
    if not quote:
        return
    found_whole = False
    at = normalized_text.find(quote)
    while at != -1:
        found_whole = True
        yield [(at, at + len(quote))]
        at = normalized_text.find(quote, at + 1)
    if found_whole:
        return
    if _ELISION.search(quote):
        pieces = [p for p in (normalize_space(s) for s in _ELISION.split(quote)) if p]
        if speech is not None and pieces and all(len(compact(p)) >= MIN_ELIDED_PIECE for p in pieces):
            yield from _elided_spans(pieces, speech)
        return
    parts = [p for p in (normalize_space(s) for s in _SENTENCE_SPLIT.split(quote)) if p]
    if len(parts) < 2:
        return
    start = normalized_text.find(parts[0])
    while start != -1:
        spans = [(start, start + len(parts[0]))]
        for part in parts[1:]:
            end = spans[-1][1]
            found = normalized_text.find(part, end)
            if found == -1 or found - end > QUOTE_PART_GAP:
                break
            spans.append((found, found + len(part)))
        else:
            yield spans
        start = normalized_text.find(parts[0], start + 1)


def quote_in_text(quote: str, normalized_text: str, speech: Speech | None = None) -> bool:
    """True when the quote appears in the minutes (see iter_quote_spans)."""
    return next(iter_quote_spans(quote, normalized_text, speech), None) is not None


_LEADING_BULLET = re.compile(r"^(?:[-*•·○●▪◦※]\s*|\d+[.)]\s+)")


def find_injection_sentences(text: str) -> list[str]:
    lines = [_LEADING_BULLET.sub("", normalize_space(s)) for s in re.split(r"\n+", text)]
    return [s for s in lines if s and any(p.search(s) for p in _INJECTION_PATTERNS)]


def _overlaps(a: str, b: str) -> bool:
    a, b = normalize_space(a), normalize_space(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size >= min(INJECTION_OVERLAP_CHARS, len(a), len(b))


def _from_injection(texts: list[str], injections: list[str]) -> bool:
    return any(_overlaps(t, s) for t in texts if t for s in injections)


def is_weekend(day: date | None) -> bool:
    return day is not None and day.weekday() >= 5


def weekend_note(day: date) -> str:
    return f"주말 기한입니다({format_due(day)}). 날짜가 맞는지 확인해 주세요."


def _parse_guess_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value.strip()) if value else None
    except ValueError:
        return None


def _parse_guess_time(value: str | None) -> time | None:
    try:
        return time.fromisoformat(value.strip()) if value else None
    except ValueError:
        return None


def _check_owner(
    raw_owner: str | None, compact_text: str, roster: Roster, notes: list[str]
) -> tuple[str | None, str | None, bool]:
    """Return (owner_name, slack_id, confirmed)."""
    raw = (raw_owner or "").strip()
    if not raw:
        notes.append("회의록에 담당자가 명시되지 않았습니다.")
        return None, None, False
    if is_group_reference(raw):
        notes.append(f"개인이 아닌 지정('{raw}')이라 담당자를 비워 두었습니다.")
        return None, None, False

    core = strip_titles(raw)
    name = person_name(raw)
    in_text = compact(raw) in compact_text or any(
        len(candidate) >= 2 and candidate in compact_text for candidate in (core, name)
    )
    if not in_text:
        notes.append(f"담당자 '{raw}'를 회의록에서 찾지 못했습니다.")
        return raw, None, False

    match = roster.match(raw)
    if match.member:
        member = match.member
        if compact(member.name) != core:
            notes.append(f"명단 매칭: '{raw}' → {member.name}")
        if not member.slack_user_id:
            notes.append("Slack 미등록")
        return member.name, member.slack_user_id, True
    if match.candidates:
        names = ", ".join(m.name for m in match.candidates)
        notes.append(f"담당자 후보가 여럿입니다: {names}")
        return raw, None, False
    if len(name) < 2:
        notes.append(f"'{raw}'만으로는 담당자를 특정할 수 없습니다.")
        return raw, None, False
    notes.append("Slack 미등록 (명단에 없음)")
    return name, None, True


def _check_due(
    item: LLMActionItem, normalized_text: str, meeting_date: date, notes: list[str]
) -> tuple[date | None, time | None, str | None, bool, bool]:
    """Return (due_date, due_time, due_text, confirmed, needs_review)."""
    due_text = (item.due_text or "").strip() or None
    guess_date = _parse_guess_date(item.due_date_guess)
    guess_time = _parse_guess_time(item.due_time_guess)
    if due_text is None:
        notes.append("회의록에 기한이 명시되지 않았습니다.")
        if guess_date:
            notes.append(f"모델이 추정한 기한({guess_date:%m/%d})은 근거 표현이 없어 쓰지 않았습니다.")
        return None, None, None, False, False

    needs_review = False
    found = quote_in_text(due_text, normalized_text)
    if not found:
        needs_review = True
        notes.append(f"기한 표현 '{due_text}'을 회의록에서 찾지 못했습니다.")
    resolved = resolve_due(due_text, meeting_date)
    notes.extend(resolved.notes)
    confirmed = resolved.confirmed and found
    if resolved.due_date is None:
        if guess_date:
            notes.append(f"모델 추정 기한은 {guess_date:%m/%d}입니다. 확인 후 입력해 주세요.")
        return None, None, due_text, False, needs_review
    if guess_date and guess_date != resolved.due_date:
        confirmed = False
        guess_label, code_label = format_due(guess_date), format_due(resolved.due_date)
        if guess_date.year != resolved.due_date.year:
            guess_label = f"{guess_date.year}년 {guess_label}"
            code_label = f"{resolved.due_date.year}년 {code_label}"
        notes.append(
            f"모델 추정({guess_label})과 코드 해석({code_label})이 달라 코드 값을 쓰고 미확정으로 두었습니다."
        )
    if resolved.due_time and guess_time and guess_time != resolved.due_time:
        confirmed = False
        notes.append(
            f"모델 추정 시각({guess_time:%H:%M})과 코드 해석({resolved.due_time:%H:%M})이 다릅니다."
        )
    if is_weekend(resolved.due_date):
        # Informational only: the confirmed status stays as decided above.
        notes.append(weekend_note(resolved.due_date))
    return resolved.due_date, resolved.due_time, due_text, confirmed, needs_review


def _build_item(
    raw: LLMActionItem,
    *,
    meeting_id: str,
    meeting_date: date,
    speech: Speech,
    compact_text: str,
    roster: Roster,
    owner_rules: OwnerRules,
    from_rules: bool,
) -> ActionItem:
    notes: list[str] = []
    evidence = raw.evidence_quote.strip()
    occurrences = list(iter_quote_spans(evidence, speech.text, speech))
    needs_review = not occurrences
    if needs_review:
        notes.append("근거 인용을 회의록에서 찾지 못했습니다.")

    owner_name, slack_id, owner_ok = _check_owner(raw.owner_name, compact_text, roster, notes)
    due_date, due_time, due_text, due_ok, due_review = _check_due(raw, speech.text, meeting_date, notes)
    co_owners = [c.strip() for c in raw.co_owners if c.strip() and compact(c) in compact_text]

    if needs_review and (owner_ok or due_ok):
        # Without verifiable evidence nothing about the item counts as confirmed.
        owner_ok = due_ok = False
        notes.append("근거를 확인할 수 없어 담당자와 기한을 확정하지 않았습니다.")
    if from_rules:
        owner_ok = due_ok = False
        notes.insert(0, "규칙 기반 추출 결과라 담당자와 기한을 직접 확인해 주세요.")
    if owner_ok and owner_name:
        note = promise_note(
            owner=owner_name,
            raw_owner=raw.owner_name or "",
            occurrences=occurrences,
            speech=speech,
            rules=owner_rules,
        )
        if note:
            owner_ok = False
            notes.append(note)
    return ActionItem(
        item_id=new_id(),
        meeting_id=meeting_id,
        task=normalize_space(raw.task),
        owner_name=owner_name,
        co_owners=co_owners,
        owner_slack_id=slack_id if owner_ok else None,
        owner_status="confirmed" if owner_ok else "unconfirmed",
        due_date=due_date,
        due_time=due_time,
        due_text=due_text,
        due_status="confirmed" if due_ok and due_date else "unconfirmed",
        evidence_quote=evidence,
        needs_review=needs_review or due_review,
        review_notes=notes,
    )


def _flag_duplicates(items: list[ActionItem]) -> None:
    for i, first in enumerate(items):
        for second in items[i + 1 :]:
            if first.owner_name != second.owner_name:
                continue
            ratio = SequenceMatcher(None, first.task, second.task).ratio()
            if ratio >= DUPLICATE_RATIO:
                second.review_notes.append(
                    f"'{first.task}'와 비슷합니다. 같은 일이면 하나를 빼 주세요."
                )


def validate_extraction(
    extraction: LLMExtraction,
    *,
    meeting_text: str,
    meeting_date: date,
    meeting_id: str,
    roster: Roster,
    from_rules: bool = False,
    owner_rules: OwnerRules | None = None,
) -> ValidatedExtraction:
    warnings: list[str] = []
    if owner_rules is None:
        owner_rules, rules_warning = default_owner_rules()
        if rules_warning:
            warnings.append(rules_warning)
    speech = Speech.from_minutes(meeting_text, owner_rules)
    normalized_text = speech.text
    compact_text = compact(meeting_text)
    injections = find_injection_sentences(meeting_text)
    dropped = 0
    if injections:
        preview = injections[0][:40] + ("…" if len(injections[0]) > 40 else "")
        warnings.append(
            f"회의록에서 AI에게 지시하는 문장 {len(injections)}건을 찾아 데이터로만 다뤘습니다: “{preview}”"
        )

    decisions: list[Decision] = []
    for raw in extraction.decisions:
        if _from_injection([raw.text, raw.evidence_quote], injections):
            dropped += 1
            continue
        decisions.append(
            Decision(
                decision_id=new_id(),
                text=normalize_space(raw.text),
                evidence_quote=raw.evidence_quote.strip(),
                needs_review=not quote_in_text(raw.evidence_quote, normalized_text, speech),
            )
        )

    items: list[ActionItem] = []
    for raw in extraction.action_items:
        if _from_injection([raw.task, raw.evidence_quote], injections):
            dropped += 1
            continue
        items.append(
            _build_item(
                raw,
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                speech=speech,
                compact_text=compact_text,
                roster=roster,
                owner_rules=owner_rules,
                from_rules=from_rules,
            )
        )
    _flag_duplicates(items)
    if dropped:
        warnings.append(f"AI 대상 지시문에서 나온 항목 {dropped}건을 결과에서 뺐습니다.")

    summary = [
        s for s in (normalize_space(x) for x in extraction.summary)
        if s and not _from_injection([s], injections)
    ][:5]
    return ValidatedExtraction(
        summary=summary,
        decisions=decisions,
        items=items,
        open_issues=[normalize_space(x) for x in extraction.open_issues if x.strip()],
        warnings=warnings,
        injection_sentences=injections,
        dropped_by_injection=dropped,
    )


def count_unconfirmed(items: list[ActionItem]) -> int:
    return sum(1 for i in items if "unconfirmed" in (i.owner_status, i.due_status))
