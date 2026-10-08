"""Whose promise is it? Deterministic owner rules (docs/DECISIONS.md, 담당자 확정 기준).

validate.py confirms that the proposed owner is a real person in the minutes.
This module then reads the quoted evidence and leaves the owner unconfirmed
when the promise belongs to an institution or is too weak to assign:

- a weak promise ("노력하겠습니다") is not an assignment;
- "우리/저희" as the subject makes it a group's or an institution's promise;
- a speaker who represents an institution (minister-level titles listed in
  data/meeting_owner_rules.yaml) promises for the institution unless they say
  "제가" themselves, and a request made to them is not an assignment.

A team member's own "~하겠습니다" and an assignment written in the minutes
("김민준 주무관이 맡기로 함") stay confirmed.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..config import REPO_ROOT
from .roster import TITLES, compact, person_name

DEFAULT_RULES_PATH = REPO_ROOT / "data" / "meeting_owner_rules.yaml"
WEAK_GAP = 8  # max characters between a weak phrase and the promise ending

# Speaker labels: '김민준 00:01:02 …' (transcript) and '한가람 위원: …' / '가상부장관 오세린: …'.
_TRANSCRIPT_LABEL = re.compile(r"([가-힣]{2,4})\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s|$)")
_COLON_LABEL = re.compile(r"◯?\s*([^:：]{2,40}?)\s*[:：](?:\s|$)")
_LABEL_TOKEN = re.compile(r"[가-힣0-9·]+")
_NAME = re.compile(r"[가-힣]{2,4}")
_BULLET = re.compile(r"[-*•·○●◦▪■□▶※]|\d+[.)]\s")

_SELF = re.compile(
    r"(?<![가-힣])(?:제가|저는|저도|저부터|저로서는|내가|나는|나도|본인이|본인은|본인도)(?![가-힣])"
)
_GROUP_NOUNS = "부처|부서|정부|기관|위원회|부|처|청|원|당|측|쪽|팀|과|실|국|회사|공사|공단|센터|본부"
_SUBJECT = "이|가|은|는|도|께서|에서|에서는|에서도|로서는|으로서는|부터"
_COLLECTIVE = re.compile(
    rf"(?<![가-힣])(?:우리|저희)들?(?:(?:{_SUBJECT})|\s?[가-힣]{{0,10}}?(?:{_GROUP_NOUNS})(?:{_SUBJECT}))"
    r"(?![가-힣])"
)
_PROMISE = re.compile(r"겠(?:습니다|다|음|어요|네요)|(?<=[가-힣])게요|기로|계획|예정|합시다")
# A concrete due date in the same sentence: a date, a weekday or "~까지".
_CONCRETE_DUE = re.compile(
    r"\d{1,4}\s*[./-]\s*\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}\s*일|[월화수목금토일]요일|까지"
)
_NEXT_WORD = re.compile(r"( ?)([가-힣]+)")
_SENTENCE_END = re.compile(r"[.?!]\s")
_PUNCT = ":：,.?!·()[]\"'“”‘’「」"
_PARTICLES = ("님께서", "께서", "님께", "에게", "께", "님", "이", "가", "은", "는", "도", "의", "과", "와")
_SUBJECT_PARTICLES = ("께서", "이", "가", "은", "는", "도")


class OwnerRulesError(ValueError):
    """The rules file cannot be read. The message is user-facing Korean."""


class OwnerRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    institution_head_titles: tuple[str, ...] = ()
    not_institution_head_titles: tuple[str, ...] = ()
    nominee_suffixes: tuple[str, ...] = ()
    weak_promise_phrases: tuple[str, ...] = ()
    weak_promise_exceptions: tuple[str, ...] = ()

    @field_validator("*", mode="before")
    @classmethod
    def _compact_entries(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            return tuple(c for c in (compact(str(v)) for v in value if v is not None) if c)
        return value

    def is_institution_head(self, title: str) -> bool:
        """'가상부장관' or '제1차관' — but not '위원장' (longest listed title wins)."""
        title = compact(title)
        for suffix in self.nominee_suffixes:
            if title.endswith(suffix) and len(title) > len(suffix):
                title = title[: -len(suffix)]
                break
        known = (*self.institution_head_titles, *self.not_institution_head_titles)
        best = max((t for t in known if title.endswith(t)), key=len, default=None)
        return best is not None and best in self.institution_head_titles

    def is_title(self, token: str) -> bool:
        token = compact(token)
        known = (*TITLES, *self.institution_head_titles, *self.not_institution_head_titles)
        return bool(token) and any(token.endswith(t) for t in known)

    def _weak_hits(self, tail: str) -> list[tuple[str, int, int]]:
        hits = []
        for phrase in self.weak_promise_phrases:
            at = tail.rfind(phrase)
            if at != -1 and len(tail) - (at + len(phrase)) <= WEAK_GAP:
                hits.append((phrase, at, at + len(phrase)))
        return hits

    def weak_phrase(self, before_promise: str) -> str | None:
        """The weak phrase right before a promise ending ('노력하도록 하'), if any."""
        hits = self._weak_hits(compact(before_promise))
        return hits[0][0] if hits else None

    def excusable(self, before_promise: str) -> bool:
        """True when every weak phrase near the promise is part of an exception ('고민해 보')."""
        tail = compact(before_promise)
        hits = self._weak_hits(tail)
        covers = [
            (at, at + len(phrase))
            for phrase in self.weak_promise_exceptions
            for at in _find_all(tail, phrase)
        ]
        return bool(hits) and all(any(c0 <= h0 and h1 <= c1 for c0, c1 in covers) for _, h0, h1 in hits)


def _find_all(text: str, part: str) -> list[int]:
    found, at = [], text.find(part)
    while at != -1:
        found.append(at)
        at = text.find(part, at + 1)
    return found


def load_owner_rules(path: Path = DEFAULT_RULES_PATH) -> OwnerRules:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return OwnerRules.model_validate(data)
    except FileNotFoundError:
        raise OwnerRulesError(f"담당자 규칙 파일이 없습니다: {path.name}") from None
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise OwnerRulesError(f"담당자 규칙 파일 형식을 확인하세요: {path.name} ({type(exc).__name__})") from None


@lru_cache(maxsize=1)
def default_owner_rules() -> tuple[OwnerRules, str | None]:
    """The shipped rules, or empty rules and a warning when the file cannot be read."""
    try:
        return load_owner_rules(), None
    except OwnerRulesError as exc:
        return OwnerRules(), f"{exc} 기관장·약한 약속 규칙 없이 담당자를 확인했습니다."


def speaker_label(line: str, rules: OwnerRules) -> str | None:
    """'한가람 위원: …' -> '한가람 위원', '김민준 00:01:02 …' -> '김민준'; None for other lines."""
    if match := _TRANSCRIPT_LABEL.match(line):
        return match.group(1)
    if match := _COLON_LABEL.match(line):
        tokens = match.group(1).split()
        if not 2 <= len(tokens) <= 4 or not all(_LABEL_TOKEN.fullmatch(t) for t in tokens):
            return None
        names = [t for t in tokens if _NAME.fullmatch(t) and not rules.is_title(t)]
        if len(names) == 1 and any(rules.is_title(t) for t in tokens):
            return " ".join(tokens)
    return None


@dataclass(frozen=True)
class Speech:
    """The minutes on one single-spaced line, with the speaker of every position.

    Lines are grouped into blocks: one utterance (a speaker label and its
    continuation lines), or, without speaker labels, one paragraph. Each
    bullet starts a new block; a blank line ends one.
    """

    text: str
    starts: tuple[int, ...]  # where each source line begins in `text`
    speakers: tuple[str | None, ...]  # the speaker label in force on that line
    blocks: tuple[int, ...] = ()  # block number of each line

    @classmethod
    def from_minutes(cls, minutes: str, rules: OwnerRules) -> Speech:
        lines: list[str] = []
        starts: list[int] = []
        speakers: list[str | None] = []
        blocks: list[int] = []
        offset, current, block, new_block = 0, None, -1, True
        for raw in minutes.splitlines():
            line = " ".join(raw.split())
            if not line:
                current, new_block = None, True  # a blank line ends an utterance or a paragraph
                continue
            label = speaker_label(line, rules)
            bullet = bool(_BULLET.match(line))
            if label:
                current = label
            elif bullet:
                current = None  # bullets and headings are the minute-taker's text
            if new_block or label or bullet:
                block += 1
            new_block = False  # continuation lines join the utterance or paragraph
            lines.append(line)
            starts.append(offset)
            speakers.append(current)
            blocks.append(block)
            offset += len(line) + 1
        return cls(" ".join(lines), tuple(starts), tuple(speakers), tuple(blocks))

    def line_of(self, offset: int) -> int:
        return max(bisect_right(self.starts, offset) - 1, 0)

    def line_end(self, index: int) -> int:
        return self.starts[index + 1] - 1 if index + 1 < len(self.starts) else len(self.text)

    def block_bounds(self, offset: int) -> tuple[int, int]:
        """[start, end) in `text` of the utterance or paragraph around `offset`."""
        if not self.starts:
            return 0, len(self.text)
        index = self.line_of(offset)
        first = last = index
        while first > 0 and self.blocks[first - 1] == self.blocks[index]:
            first -= 1
        while last + 1 < len(self.starts) and self.blocks[last + 1] == self.blocks[index]:
            last += 1
        return self.starts[first], self.line_end(last)


@dataclass(frozen=True)
class _Segment:
    speaker: str | None
    start: int
    end: int


@dataclass(frozen=True)
class _Marker:
    kind: str  # "self" (제가), "named" (owner's name as the subject) or "group" (저희가)
    text: str
    at: int


def _segments(speech: Speech, spans: Iterable[tuple[int, int]]) -> list[_Segment]:
    """Split quote spans where the speaker changes."""
    out: list[_Segment] = []
    for start, end in spans:
        index = speech.line_of(start)
        while start < end and index < len(speech.starts):
            piece_end = min(end, speech.line_end(index))
            if piece_end > start:
                out.append(_Segment(speech.speakers[index], start, piece_end))
            index += 1
            if index < len(speech.starts):
                start = max(start, speech.starts[index])
    return out


def _clean(token: str) -> str:
    token = token.strip(_PUNCT)
    for _ in range(3):
        for particle in _PARTICLES:
            if token.endswith(particle) and len(token) - len(particle) >= 2:
                token = token[: -len(particle)]
                break
        else:
            break
    return token


def _speaks(label: str, names: set[str]) -> bool:
    return any(token in names for token in label.split())


def _head_title(names: set[str], raw_owner: str, speech: Speech, rules: OwnerRules) -> str | None:
    """An institution-head title written next to the owner's name, if any."""
    titles = [t for t in raw_owner.split() if rules.is_title(t)]
    tokens = speech.text.split(" ")
    for index, token in enumerate(tokens):
        if token.strip(_PUNCT) in names or _clean(token) in names:  # '정하은' keeps its last syllable
            for near in (index - 1, index + 1):
                if 0 <= near < len(tokens):
                    titles.append(_clean(tokens[near]))
    return next((t for t in titles if rules.is_institution_head(t)), None)


def _named_subject(text: str, end: int, rules: OwnerRules) -> bool:
    """True when the name ending at `end` is the subject: '김민준이', '김민준 주무관이', '이서연 사무관님이'."""
    match = _NEXT_WORD.match(text, end)
    if not match:
        return False
    spaced, word = match.groups()
    particle = next((p for p in _SUBJECT_PARTICLES if word.endswith(p)), None)
    if particle is None:
        return False
    stem = word[: -len(particle)].removesuffix("님")
    if not stem:
        return not spaced
    return bool(spaced) and rules.is_title(stem)


def _markers(
    speech: Speech, segment: _Segment, *, own: bool, owner_forms: set[str], rules: OwnerRules
) -> list[_Marker]:
    found = [
        _Marker("group", m.group(0), m.start())
        for m in _COLLECTIVE.finditer(speech.text, segment.start, segment.end)
    ]
    if own:  # "제가" refers to the owner only in the owner's own words
        found += [
            _Marker("self", m.group(0), m.start()) for m in _SELF.finditer(speech.text, segment.start, segment.end)
        ]
    for form in owner_forms:
        pattern = re.compile(rf"(?<![가-힣]){re.escape(form)}")
        for m in pattern.finditer(speech.text, segment.start, segment.end):
            if _named_subject(speech.text, m.end(), rules):
                found.append(_Marker("named", m.group(0), m.start()))
    return found


def _sentences(text: str, start: int, end: int) -> list[tuple[int, int]]:
    out = []
    for match in _SENTENCE_END.finditer(text, start, end):
        out.append((start, match.start() + 1))
        start = match.end()
    if start < end:
        out.append((start, end))
    return out


def _promise_subject(
    speech: Speech, scope: list[_Segment], *, own: bool, owner_forms: set[str], rules: OwnerRules
) -> _Marker | None:
    """Who makes the last promise: the last subject in its own sentence, else the subject of
    the same speaker's previous promise ('저희가 검토하겠습니다. 결과는 보고드리겠습니다.').
    Subjects of other sentences ('우리가 15일까지 정하게 되어 있습니다.') do not count.
    Without any promise ending, the last subject in the quote."""
    subject = carried = None
    speaker: str | None = None
    seen_promise = False
    everything: list[_Marker] = []
    for segment in scope:
        if segment.speaker != speaker:  # a subject carries over only within one speaker's words
            speaker, carried = segment.speaker, None
        for start, end in _sentences(speech.text, segment.start, segment.end):
            sentence = _Segment(segment.speaker, start, end)
            markers = _markers(speech, sentence, own=own, owner_forms=owner_forms, rules=rules)
            everything += markers
            endings = [m.start() for m in _PROMISE.finditer(speech.text, start, end)]
            if not endings:
                continue
            seen_promise = True
            carried = max((m for m in markers if m.at < endings[-1]), key=lambda m: m.at, default=carried)
            subject = carried
    if not seen_promise:
        return max(everything, key=lambda m: m.at, default=None)
    return subject


def _sentence_bounds(text: str, start: int, end: int, at: int) -> tuple[int, int]:
    """The sentence of [start, end) that contains position `at`."""
    begin = start
    for match in _SENTENCE_END.finditer(text, start, at):
        begin = match.end()
    following = _SENTENCE_END.search(text, at, end)
    return begin, following.start() + 1 if following else end


def _weak_phrase(speech: Speech, segment: _Segment, at: int, rules: OwnerRules, *, own: bool) -> str | None:
    """The weak phrase of the promise ending at `at`, unless the speaker commits to it.

    '검토해 보겠습니다' is weak, but '제가 금요일까지 검토해 보겠습니다' is not: an
    exception phrase counts as a commitment when the owner's own sentence also
    has a first-person subject and a concrete due date.
    """
    begin, finish = _sentence_bounds(speech.text, segment.start, segment.end, at)
    phrase = rules.weak_phrase(speech.text[begin:at])
    if phrase is None:
        return None
    sentence = speech.text[begin:finish]
    if own and rules.excusable(speech.text[begin:at]) and _SELF.search(sentence) and _CONCRETE_DUE.search(sentence):
        return None
    return phrase


def promise_note(
    *,
    owner: str,
    raw_owner: str,
    occurrences: list[list[tuple[int, int]]],
    speech: Speech,
    rules: OwnerRules,
) -> str | None:
    """Why this owner stays unconfirmed (a Korean review note), or None for a person's own task.

    `occurrences` are the places the evidence quote was found, each a list of
    spans in `speech.text`; the one inside the owner's own words is preferred.
    """
    if not occurrences:
        return None
    names = {n for n in (owner, person_name(raw_owner)) if len(n) >= 2}
    owner_forms = names | ({" ".join(raw_owner.split())} if len(compact(raw_owner)) >= 2 else set())
    candidates = [_segments(speech, spans) for spans in occurrences]
    segments = next(
        (c for c in candidates if any(s.speaker and _speaks(s.speaker, names) for s in c)), candidates[0]
    )
    own = [s for s in segments if s.speaker and _speaks(s.speaker, names)]
    scope = own or segments

    endings: list[tuple[int, _Segment]] = [
        (m.start(), s) for s in scope for m in _PROMISE.finditer(speech.text, s.start, s.end)
    ]
    weak = [_weak_phrase(speech, segment, at, rules, own=bool(own)) for at, segment in endings]
    if weak and all(weak):
        return f"'{weak[-1]}' 같은 약한 약속이라 담당자를 확정하지 않았습니다."

    subject = _promise_subject(speech, scope, own=bool(own), owner_forms=owner_forms, rules=rules)
    if subject and subject.kind == "group":
        return f"주어가 '{subject.text}'(기관·집단)라 개인 담당자로 확정하지 않았습니다."

    head = _head_title(names, raw_owner, speech, rules)
    if head and any(s.speaker for s in segments):
        if not own:
            return f"기관 대표({head})에게 한 요청이고, 본인이 맡겠다고 한 발언이 근거에 없어 확정하지 않았습니다."
        if not subject:
            return (
                f"기관 대표 발언자({head})의 약속에 '제가' 같은 1인칭 주어가 없어 "
                "기관의 약속으로 보고 담당자를 확정하지 않았습니다."
            )
    return None
