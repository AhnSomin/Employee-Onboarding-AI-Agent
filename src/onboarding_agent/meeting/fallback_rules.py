"""Rule-based extraction for when no LLM is available or FORCE_FALLBACK is on (spec 8.6).

Nothing is generated: tasks, decisions and the summary are lines copied from
the minutes. Every result is later marked unconfirmed so a person reviews it.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from .dates import find_due_text
from .models import LLMActionItem, LLMDecision, LLMExtraction
from .roster import Roster

Section = Literal["action", "decision", "other"]

_TITLES = r"(?:주무관|사무관|서기관|과장|팀장|계장)"
_DECISION = re.compile(r"결정|확정|합의|하기로")
_INTENT = re.compile(r"기로\s*(?:함|했|하였|합니다|하자|하죠|해요)|도록\s*(?:함|하자|하죠)")
_RECURRING = re.compile(r"매주|매일|매월|매달|격주")
_OWNER_LABEL = re.compile(rf"담당\s*자?\s*[:：]\s*(?P<name>[가-힣]{{1,4}}(?:\s*{_TITLES})?)")
_ITEM_OWNER = re.compile(rf"[:：]\s*(?P<name>[가-힣]{{2,4}})\s*{_TITLES}?\s*님?\s*[,，]")
_OWNER_TITLED = re.compile(
    rf"(?P<name>[가-힣]{{1,4}})\s*(?P<title>{_TITLES}\s*님?|님)(?:이|가|께서|은|는)(?=\s|$)"
)
_OWNER_PLAIN = re.compile(r"(?P<name>[가-힣]{2,4})(?:이|가|께서)(?=\s)")
_OWNER_VERB = re.compile(
    r"맡|담당|진행|준비|작성|정리|취합|공유|전달|검토|제출|올리|게시|확인|만들|보내|알아"
)
_SELF_ASSIGN = re.compile(r"제가\s.*?(?:게요|께요|겠습니다|겠어요)")
_TIMESTAMPED = re.compile(
    r"^(?P<speaker>[가-힣]{2,4})\s+\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s+(?P<utterance>.+)$"
)
_COLON_SPEAKER = re.compile(r"^(?P<speaker>[가-힣]{2,4})\s*[:：]\s*(?P<utterance>.+)$")
_LABEL_LINE = re.compile(r"^(?:참석자?|일시|일자|날짜|장소|회의\s*명|회의\s*제목|작성자?|배포)\s*[:：]")
_BULLET = re.compile(r"^(?:[-*•·○●▪◦]\s*|\d+[.)]\s+|[가-하][.)]\s+)")
_HEADER = re.compile(
    r"^(?:[#■□○●◆▶\[【]\s*)?(?:\d+[.)]\s*)?(?P<name>[^:：\n]{1,20}?)\s*[\]】]?\s*[:：]?\s*$"
)
_ACTION_HEADER = re.compile(r"액션|할\s*일|후속|조치|TODO|To-?Do|과제", re.IGNORECASE)
_DECISION_HEADER = re.compile(r"결정|합의")
_OTHER_HEADER = re.compile(r"안건|논의|공유|정리|기타|비고|미결|참고|보고")
_MIN_SELF_ASSIGN_LEN = 12


def _section_of(line: str) -> Section | None:
    """Section kind when the line is a heading such as "4. 액션 아이템" or "○ 기타"."""
    if line.startswith(("-", "*", "•", "·")):
        return None
    match = _HEADER.match(line)
    if not match:
        return None
    name = match["name"]
    if _ACTION_HEADER.search(name):
        return "action"
    if _DECISION_HEADER.search(name):
        return "decision"
    if _OTHER_HEADER.search(name):
        return "other"
    return None


def _split_speaker(line: str, roster: Roster) -> tuple[str | None, str]:
    timestamped = _TIMESTAMPED.match(line)
    if timestamped:
        return timestamped["speaker"], timestamped["utterance"].strip()
    colon = _COLON_SPEAKER.match(line)
    if colon and roster.match(colon["speaker"]).member:
        return colon["speaker"], colon["utterance"].strip()
    return None, line


def _find_owner(unit: str, speaker: str | None, section: Section, roster: Roster) -> str | None:
    label = _OWNER_LABEL.search(unit)
    if label:
        return label["name"].strip()
    if section == "action":
        item_owner = _ITEM_OWNER.search(unit)
        if item_owner:
            return item_owner["name"]
    titled = _OWNER_TITLED.search(unit)
    if titled and _OWNER_VERB.search(unit[titled.end() :]):
        return f"{titled['name']} {titled['title']}".strip()
    for plain in _OWNER_PLAIN.finditer(unit):
        if roster.match(plain["name"]).member and _OWNER_VERB.search(unit[plain.end() :]):
            return plain["name"]
    if speaker and _SELF_ASSIGN.search(unit):
        return speaker
    return None


def extract_with_rules(text: str, *, meeting_date: date, roster: Roster) -> LLMExtraction:
    """Copy decision and action lines out of the minutes without any generation.

    `meeting_date` is accepted for symmetry with the LLM path; due expressions
    are resolved later by the validator.
    """
    del meeting_date
    decisions: list[LLMDecision] = []
    items: list[LLMActionItem] = []
    section: Section = "other"

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("※") or _LABEL_LINE.match(line):
            continue
        heading = _section_of(line)
        if heading:
            section = heading
            continue
        speaker, utterance = _split_speaker(line, roster)
        unit = _BULLET.sub("", utterance).strip()
        if not unit:
            continue

        if section == "decision" or _DECISION.search(unit):
            decisions.append(LLMDecision(text=unit, evidence_quote=unit))

        owner = _find_owner(unit, speaker, section, roster)
        due_text = find_due_text(unit)
        if owner and owner == speaker and due_text is None and len(unit) < _MIN_SELF_ASSIGN_LEN:
            continue  # "네, 제가 할게요." carries no task of its own
        intends = "까지" in unit or bool(_INTENT.search(unit))
        if not owner and _RECURRING.search(unit):
            continue  # a recurring arrangement ("매주 목요일") is a decision, not a task
        if section == "action" or owner or (due_text and intends):
            items.append(
                LLMActionItem(task=unit, owner_name=owner, due_text=due_text, evidence_quote=unit)
            )

    return LLMExtraction(
        title_suggestion=None,
        summary=[d.text for d in decisions[:5]],
        decisions=decisions,
        action_items=items,
    )
