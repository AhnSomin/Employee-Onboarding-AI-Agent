"""Whose promise is it? Deterministic owner rules (docs/DECISIONS.md, 담당자 확정 기준).

validate.py confirms that the proposed owner is a real person in the minutes.
This module then reads the quoted evidence and leaves the owner unconfirmed
when the promise belongs to an institution or is too weak to assign:

- a weak promise ("노력하겠습니다") is not an assignment;
- "우리/저희" as the subject makes it a group's or an institution's promise;
- a speaker who represents an institution (minister-level and local-government
  head titles listed in data/meeting_owner_rules.yaml) promises for the
  institution unless they say "직접" in that promise ("제가 직접 챙기겠습니다"),
  and a request made to them is not an assignment.

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

# Speaker labels: '김민준 00:01:02 …' (transcript) and '한가람 위원: …' / '가상부장관 오세린: …'.
_TRANSCRIPT_LABEL = re.compile(r"([가-힣]{2,4})\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s|$)")
_COLON_LABEL = re.compile(r"(?:[◯○●◦▪■□▶※*•·-]\s*)?([^:：]{2,40}?)\s*[:：]")
_LABEL_NOTE = re.compile(r"\((?:사회|진행|사회자|위원장 대리)\)")  # '박지훈 팀장(사회):'
_LABEL_PAREN = re.compile(r"([가-힣]{2,4})\(([가-힣0-9·]{2,30})\)")  # '문해솔(가상부장관):'
_LABEL_TOKEN = re.compile(r"[가-힣0-9·]+")
_NAME = re.compile(r"[가-힣]{2,4}")
_BULLET = re.compile(r"[-*•·○●◦▪■□▶※]|\d+[.)]\s")

_SELF = re.compile(
    r"(?<![가-힣])(?:제가|저는|저도|저부터|저로서는|내가|나는|나도|본인이|본인은|본인도)(?![가-힣])"
)
_GROUP_NOUNS = (
    "부처|부서|정부|기관|위원회|부|처|청|원|당|측|쪽|팀|과|실|국|회사|공사|공단|센터|본부|시|도|군|구"
    "|실무진|직원들?|팀원들?|위원들?|TF|태스크포스"
)
_SUBJECT = "이|가|은|는|도|께서|에서|에서는|에서도|로서는|으로서는|부터|끼리|둘이|셋이|모두"
_SOON = r"(?:이번|다음|내일|오늘|모레|금주|차주|\d|[월화수목금]요일|중으로|안에|직접|함께|같이)"
_COLLECTIVE = re.compile(
    rf"(?<![가-힣])(?:우리|저희)들?"
    rf"(?:(?:{_SUBJECT})요?(?![가-힣])"
    rf"|\s?(?:[가-힣]{{1,6}}\s)?[가-힣A-Za-z]{{0,10}}?(?:{_GROUP_NOUNS})(?:(?:{_SUBJECT})요?(?![가-힣])|(?=\s{_SOON}))"
    rf"|\s(?:둘이|셋이|모두|다같이)(?![가-힣])"
    rf"|(?=\s{_SOON}))"
)
_PROMISE = re.compile(
    r"겠(?:습니다|다|음|어요|네요|고|으며)|(?<=[가-힣])게요|합시다|봅시다|(?<=[하보])죠|(?<=[할볼])게(?![가-힣])"
    r"|(?<=[가-힣])기로\s*(?:하(?!였던|였었)|함|했(?!던|었)|합|결정|한(?!다))"  # not '하기로 했던' (a plan given up)
    r"|계획(?:입니다|이다|임|이고|이에요)|예정(?:입니다|이다|임|이고|이에요)"
    r"|려고\s*합니다|고자\s*합니다|(?:할|볼|드릴)\s*생각입니다"
)
# '겠' that is not the speaker's promise: '알겠습니다', '어렵겠습니다만', '감사하겠습니다', '보시겠어요?'.
_NOT_PROMISE_STEM = ("알", "없", "어렵", "감사하", "좋", "모르", "같", "되", "힘들", "괜찮", "시", "싶")
# A concrete due date in the same sentence: a date, a weekday, or "~까지" after a day word.
_CONCRETE_DUE = re.compile(
    r"(?<!\d)(?:\d{4}\s*[.-]\s*)?(?:1[0-2]|0?[1-9])\s*[./]\s*(?:3[01]|[12]\d|0?[1-9])\.?"
    r"(?:\s*\([월화수목금토일]\))?\s*(?:까지|이내|전|중|안)"
    r"|\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}\s*일\s*(?:\([^)]*\))?\s*(?:까지|이내|안에|전까지|중)"
    r"|[월화수목금토일]요일"
    r"|(?:오늘|내일|모레|글피|이번\s*주|다음\s*주|금주|차주|주말|이달|이번\s*달|다음\s*달|월말|연말)"
    r"(?:\s*(?:초|중|말|아침|오전|오후|저녁|\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?)){0,2}\s*(?:까지|중|안에)"
)
# Spelling variants folded before weak phrases are matched (on compacted text):
# '참고하도록 하겠' / '참고토록 하겠' / '참고를 하겠' -> '참고하겠',
# '할게요' -> '하겠어요', '검토를 해 볼' -> '검토해보'.
_FOLDS = (
    (re.compile(r"(?<=[가-힣])(?:을|를)(?=하(?:겠|도록)|해(?:보|봐|볼)|할게|할께)"), ""),
    (re.compile(r"(?:하도록|토록)(?=하겠|할게|할께)"), ""),
    (re.compile(r"(?<=[가-힣])하는(?:것으로|걸로)(?=하겠|할게|할께)"), ""),
    (re.compile(r"(?<=[가-힣])(?:을|를)?(?:좀|더|조금|한번|다시)+(?=해(?:보|봐|볼))"), ""),
    (re.compile(r"해(?:볼|봐)"), "해보"),
    (re.compile(r"할[게께]요"), "하겠어요"),
    (re.compile(r"보[게께]요"), "보겠어요"),
)
# What may stand between a weak phrase and the promise ending: particles and auxiliary verbs only
# ('노력을 기울이', '노력해 나가', '생각해 보도록 하'), not another object or verb ('노력의 결과를 정리하').
_WEAK_TAIL = re.compile(
    r"(?:을|를|의|이|은|는|도|만|로|좀|더|한번|다시|조금|계속|잘|꼭"
    r"|하|해|해서|하여|하고|할|나가|가|보|도록|기울이|다|드리|토록|야|어야|아야|겠|두|있|삼|아끼지않)*"
)
# '직접' as the speaker's own act; not a noun compound ('직접적', '직접고용', '직접 투자').
_DIRECT = re.compile(
    r"직접(?!(?:적|고용|비|세|투자|선거|민주|경비|생산|구매|수입|수출|원인|증거|관련|연관|피해|흡연|화법|교섭|지불)"
    r"|\s+(?:고용|투자|선거|생산|구매|수입|수출)(?![가-힣]*하겠))"
)
# Negation of the act '직접' introduces, within the next three words: '직접 챙기기는 어렵겠습니다만',
# '직접 하겠다는 건 아니고', '직접 나서기보다' — not '늦지 않게', '보다 꼼꼼하게'.
_NOT_DIRECT = re.compile(
    r"어렵|힘들|곤란|못|수\s*(?:는|가)?\s*없|아니|대신|말고|(?<=[가-힣])보다|않(?!도록|게|으면|고는)"
)
_SELF_WORDS = ("제가", "저는", "저도", "내가", "나는", "본인이", "본인은", "본인도")
_ROLE_WORDS = ("실무진", "실무자", "직원", "직원들", "담당자", "담당관", "부서", "실무부서", "팀", "과", "국", "실")
_HEADING_WORDS = frozenset(
    (
        "협의 동향 지원 현황 계획 보고 결과 일정 안건 사항 기타 논의 결정 요청 예산 회의 공지 참고 비고 "
        "일시 장소 내용 주제 목적 배경 문제 대책 방안 검토 추진 조치 점검 관련 담당 확인 정리 공유 협조 "
        "준비 회신 제출 진행 상황 요약"
    ).split()
)
_NEXT_WORD = re.compile(r"( ?)([가-힣]+)")
_SENTENCE_END = re.compile(r"(?<![\d.])[.?!](?!\.)\s")  # not '10. 14.' or '제가... 금요일'
_ADNOMINAL = ("은", "는", "던", "한", "린", "든", "낸", "준", "된", "운", "쓴", "본", "단", "될", "할")

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

    def _weak_hits(self, window: str, ending_at: int) -> list[tuple[str, int, int]]:
        """Weak phrases that are the predicate of the promise ending at `ending_at`.

        `window` is the folded, compacted sentence up to and including the ending, so an
        entry may include the ending itself ('참고하겠'). Between a phrase and the ending
        only particles and auxiliary verbs may stand ('노력을 기울이겠습니다').
        """
        hits = []
        for phrase in self.weak_promise_phrases:
            at = window.rfind(phrase)
            if at == -1:
                continue
            end = at + len(phrase)
            if end >= ending_at or _WEAK_TAIL.fullmatch(window, end, ending_at):
                hits.append((phrase, at, end))
        return hits

    def weak_phrase(self, window: str, ending_at: int) -> str | None:
        """The weak phrase of the promise ending at `ending_at` in `window` ('노력하겠'), if any."""
        hits = self._weak_hits(window, ending_at)
        return hits[0][0] if hits else None

    def excusable(self, window: str, ending_at: int) -> bool:
        """True when every weak phrase of the promise is part of an exception ('고민해 보')."""
        hits = self._weak_hits(window, ending_at)
        covers = [
            (at, at + len(phrase))
            for phrase in self.weak_promise_exceptions
            for at in _find_all(window, phrase)
        ]
        return bool(hits) and all(any(c0 <= h0 and h1 <= c1 for c0, c1 in covers) for _, h0, h1 in hits)


def promise_window(sentence: str, ending: str) -> tuple[str, int]:
    """The compacted, folded sentence through its promise ending, and where the ending starts."""
    head, tail = compact(sentence[: len(sentence) - len(ending)]), compact(ending)
    window = head + tail
    for pattern, replacement in _FOLDS:
        window = pattern.sub(replacement, window)
    return window, max(len(window) - len(tail), 0)


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


def _label_title(token: str, rules: OwnerRules) -> bool:
    """A title in a speaker label. Words with digits are not titles ('2고사장'), except '제1차관'."""
    if re.search(r"\d", re.sub(r"제\d+", "", token)):
        return False
    return rules.is_title(token)


def speaker_label(line: str, rules: OwnerRules) -> str | None:
    """'한가람 위원: …' -> '한가람 위원', '김민준 00:01:02 …' -> '김민준'; None for other lines.

    Also '○ 김민준 주무관: …', '인사팀 김민준 주무관: …', '가상시 시장 윤채원: …',
    '문해솔(가상부장관): …' and '박지훈 팀장(사회): …'. Headings such as '광역시장 협의:' are not labels.
    """
    if match := _TRANSCRIPT_LABEL.match(line):
        return match.group(1)
    if not (match := _COLON_LABEL.match(line)):
        return None
    label = _LABEL_PAREN.sub(r"\1 \2", _LABEL_NOTE.sub("", match.group(1)))
    tokens = label.split()
    if not 2 <= len(tokens) <= 4 or not all(_LABEL_TOKEN.fullmatch(t) for t in tokens):
        return None
    titled = [_label_title(t, rules) for t in tokens]
    candidates = [i for i, t in enumerate(tokens) if _NAME.fullmatch(t) and not titled[i] and t not in _HEADING_WORDS]
    if not any(titled) or not candidates:
        return None
    if len(candidates) == 1:
        return " ".join(tokens)
    if titled[-1] and not titled[-2] and len(tokens) - 2 in candidates:
        return " ".join(tokens)  # '인사팀 김민준 주무관': the name stands right before the closing title
    after_title = [i for i in candidates if i > 0 and titled[i - 1]]
    return " ".join(tokens) if len(after_title) == 1 else None  # '가상시 시장 윤채원'


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
            bullet = bool(_BULLET.match(line)) and not label
            if label:
                current = label
            elif bullet and (new_block or current is None):
                current = None  # bullets and headings are the minute-taker's text
            elif bullet:
                bullet = False  # a numbered point inside someone's utterance stays theirs
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


def _head_title(
    names: set[str], raw_owner: str, speech: Speech, rules: OwnerRules, *, known_member: bool = False
) -> str | None:
    """An institution-head title of the owner.

    Titles come from the owner label and the owner's own speaker labels. When a speaker label
    of the owner has a title, it decides alone ('정하은 주무관'), so words such as '동부시장' or
    '시장 조사' near the name do not turn a team member into a head; a roster member (the team)
    is never made a head by such words either. Otherwise '이름(직함)' and the running text count:
    the title right after the bare name ('홍길동 차관이'), else right before it ('가상부장관 홍길동').
    """
    titles = [t for t in raw_owner.split() if rules.is_title(t)]
    label_titles = [
        t for label in {s for s in speech.speakers if s} if _speaks(label, names)
        for t in label.split() if rules.is_title(t)
    ]
    titles += label_titles
    if not label_titles and not known_member:
        for name in names:
            for match in re.finditer(rf"{re.escape(name)}\s*\(([^)]{{2,30}})\)", speech.text):
                titles.append(compact(match.group(1)))
        for index, start in enumerate(speech.starts):
            tokens = speech.text[start : speech.line_end(index)].split(" ")
            for i, token in enumerate(tokens):
                bare = token.strip(_PUNCT) in names and not token.endswith((",", ":", "："))
                if not bare and _clean(token) not in names:  # '정하은' keeps its last syllable
                    continue
                after = _clean(tokens[i + 1]) if bare and i + 1 < len(tokens) else ""
                previous = tokens[i - 1].strip(_PUNCT) if i > 0 else ""
                closed = i == 0 or tokens[i - 1].endswith((".", "?", "!", ":", "：", ","))
                # a title before a name takes no particle: '가상부장관 홍길동', not '동부시장은 홍길동'
                before = "" if closed or _clean(previous) != previous else previous
                if after and rules.is_title(after):
                    titles.append(after)
                elif before and rules.is_title(before):
                    titles.append(before)
    return next((t for t in titles if rules.is_institution_head(t)), None)


def _named_subject(text: str, end: int, rules: OwnerRules) -> bool:
    """True when the name ending at `end` is the subject: '김민준이', '김민준 주무관이', '이서연 사무관님이',
    '김민준 씨가'."""
    match = _NEXT_WORD.match(text, end)
    if not match:
        return False
    spaced, word = match.groups()
    particle = next((p for p in _SUBJECT_PARTICLES if word.endswith(p)), None)
    if particle is None:
        return False
    stem = word[: -len(particle)].removesuffix("님").removesuffix("씨")
    if not stem:
        return True
    return bool(spaced) and rules.is_title(stem)


def _relative_clause(text: str, end: int) -> bool:
    """True when the word after `end` modifies a noun ('저희 팀이 정리한 자료', '제가 말씀드린 일정'),
    so the marker before it is not the subject of the promise."""
    match = _NEXT_WORD.match(text, end)
    return bool(match) and match.group(1) == " " and len(match.group(2)) >= 2 and match.group(2).endswith(_ADNOMINAL)


def _markers(
    speech: Speech, segment: _Segment, *, own: bool, owner_forms: set[str], rules: OwnerRules
) -> list[_Marker]:
    found = [
        _Marker("group", m.group(0), m.start())
        for m in _COLLECTIVE.finditer(speech.text, segment.start, segment.end)
        if not _relative_clause(speech.text, m.end())
    ]
    if own:  # "제가" refers to the owner only in the owner's own words
        found += [
            _Marker("self", m.group(0), m.start())
            for m in _SELF.finditer(speech.text, segment.start, segment.end)
            if not _relative_clause(speech.text, m.end())
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


def _sentence_bounds(text: str, start: int, end: int, at: int) -> tuple[int, int]:
    """The sentence of [start, end) that contains position `at`."""
    begin = start
    for match in _SENTENCE_END.finditer(text, start, at):
        begin = match.end()
    following = _SENTENCE_END.search(text, at, end)
    return begin, following.start() + 1 if following else end


def _promises(text: str, start: int, end: int) -> list[re.Match[str]]:
    """Promise endings in [start, end), without '겠' forms that are not the speaker's promise:
    '알겠습니다', '어렵겠습니다만', '감사하겠습니다', '보시겠어요?', '하겠다고 했습니다'."""
    found = []
    for match in _PROMISE.finditer(text, start, end):
        ending = match.group(0)
        if ending.startswith("겠"):
            before = compact(text[max(start, match.start() - 6) : match.start()])
            if before.endswith(_NOT_PROMISE_STEM):
                continue
            if ending == "겠다" and text[match.end() : match.end() + 1] in ("고", "는", "며", "면"):
                continue
        close = re.search(r"[.?!]", text[match.end() : end])
        if close and close.group(0) == "?":
            continue  # a question
        found.append(match)
    return found


def _sentence_scope(speech: Speech, segments: list[_Segment]) -> list[_Segment]:
    """The quote's pieces widened to whole sentences of the same utterance, so a promise ending
    or a '저희' subject just outside the quote still counts."""
    widened = []
    for segment in segments:
        low, high = speech.block_bounds(segment.start)
        start, _ = _sentence_bounds(speech.text, low, high, segment.start)
        _, end = _sentence_bounds(speech.text, low, high, max(segment.start, segment.end - 1))
        if widened and widened[-1].speaker == segment.speaker and widened[-1].end >= start:
            widened[-1] = _Segment(segment.speaker, widened[-1].start, max(widened[-1].end, end))
        else:
            widened.append(_Segment(segment.speaker, start, end))
    return widened


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
            endings = [m.start() for m in _promises(speech.text, start, end)]
            if not endings:
                continue
            seen_promise = True
            carried = max((m for m in markers if m.at < endings[-1]), key=lambda m: m.at, default=carried)
            subject = carried
    if not seen_promise:
        return max(everything, key=lambda m: m.at, default=None)
    return subject


def _weak_phrase(
    speech: Speech, segment: _Segment, ending: re.Match[str], rules: OwnerRules, *, own: bool, head: bool
) -> str | None:
    """The weak phrase of a promise, unless the speaker commits to it.

    '검토해 보겠습니다' is weak, but '제가 금요일까지 검토해 보겠습니다' is not: an
    exception phrase counts as a commitment when the owner's own sentence also
    has a first-person subject and a concrete due date. This exception is not
    for institution heads.
    """
    begin, finish = _sentence_bounds(speech.text, segment.start, segment.end, ending.start())
    window, ending_at = promise_window(speech.text[begin : ending.end()], ending.group(0))
    phrase = rules.weak_phrase(window, ending_at)
    if phrase is None:
        return None
    sentence = speech.text[begin:finish]
    if (
        own
        and not head
        and rules.excusable(window, ending_at)
        and _SELF.search(sentence)
        and _CONCRETE_DUE.search(sentence)
    ):
        return None
    return phrase


def _says_directly(speech: Speech, ending: re.Match[str], rules: OwnerRules) -> bool:
    """True when the minutes sentence of this promise says '직접' as the speaker's own act.

    '직접' does not count when it is negated ('직접 챙기기는 어렵겠습니다만'), belongs to someone
    else ('담당 실장이 내일 직접', '위원님께서 직접') or is part of a noun ('직접고용')."""
    low, high = speech.block_bounds(ending.start())
    begin, finish = _sentence_bounds(speech.text, low, high, ending.start())
    for match in _DIRECT.finditer(speech.text, begin, finish):
        if _someone_else(speech.text[begin : match.start()].split(), rules):
            continue
        following = " ".join(speech.text[match.end() : finish].split()[:3])
        if _NOT_DIRECT.search(following):
            continue
        return True
    return False


def _requested_of(speech: Speech, segments: list[_Segment], owner_forms: set[str]) -> bool:
    """True when the minute-taker wrote a request to the owner ('문해솔 장관에게 … 보고를 요청함')."""
    for segment in segments:
        if segment.speaker:
            continue
        text = speech.text[segment.start : segment.end]
        if not any(f in text for f in owner_forms):
            continue
        if re.search(r"(?:줄\s*것을|주도록|하도록|할\s*것을|달라고|주기를)\s*요청", text):
            return True  # '위원들은 윤채원 시장이 … 보고해 줄 것을 요청함'
        if "요청" in text and any(re.search(rf"{re.escape(f)}\S*(?:에게|께)(?![가-힣])", text) for f in owner_forms):
            return True
    return False


def _assigned_elsewhere(
    speech: Speech, segments: list[_Segment], own: list[_Segment], owner_forms: set[str], rules: OwnerRules
) -> bool:
    """True when the quote also holds an explicit assignment naming the owner as the subject of a
    firm promise outside the owner's own words ('김민준 주무관이 정리하기로 함' + '네, 최선을 다하겠습니다')."""
    for segment in segments:
        if segment in own:
            continue
        for start, end in _sentences(speech.text, segment.start, segment.end):
            sentence = _Segment(segment.speaker, start, end)
            named = [m for m in _markers(speech, sentence, own=False, owner_forms=owner_forms, rules=rules)
                     if m.kind == "named"]
            if named and _promises(speech.text, start, end):
                return True
    return False


def _someone_else(before: list[str], rules: OwnerRules) -> bool:
    """True when the nearest subject before '직접' is another person or body, not the speaker."""
    for token in reversed(before):
        word = token.strip(_PUNCT)
        if word in _SELF_WORDS:
            return False
        if word.endswith(("께서", "님이", "님께서", "님은", "님도", "님")):
            return True
        if word.endswith(("이", "가", "은", "는", "도", "에서", "에서는")):
            stem = _clean(word).removesuffix("에서는").removesuffix("에서")
            if stem in _ROLE_WORDS or rules.is_title(stem):
                return True
    return False


def promise_note(
    *,
    owner: str,
    raw_owner: str,
    occurrences: list[list[tuple[int, int]]],
    speech: Speech,
    rules: OwnerRules,
    known_member: bool = False,
) -> str | None:
    """Why this owner stays unconfirmed (a Korean review note), or None for a person's own task.

    `occurrences` are the places the evidence quote was found, each a list of
    spans in `speech.text`; the one inside the owner's own words is preferred.
    `known_member` is True for a roster (team) member.
    """
    if not occurrences:
        return None
    names = {n for n in (owner, person_name(raw_owner)) if len(n) >= 2}
    owner_forms = names | ({" ".join(raw_owner.split())} if len(compact(raw_owner)) >= 2 else set())
    candidates = [_segments(speech, spans) for spans in occurrences]
    segments = next(
        (c for c in candidates if any(s.speaker and _speaks(s.speaker, names) for s in c)), candidates[0]
    )
    segments = _sentence_scope(speech, segments)
    own = [s for s in segments if s.speaker and _speaks(s.speaker, names)]
    scope = own or segments
    speech_only = bool(segments) and all(s.speaker for s in segments)

    head = _head_title(names, raw_owner, speech, rules, known_member=known_member)
    endings = [(m, s) for s in scope for m in _promises(speech.text, s.start, s.end)]
    weak = [_weak_phrase(speech, s, m, rules, own=bool(own), head=bool(head)) for m, s in endings]
    if weak and all(weak) and not (own and not head and _assigned_elsewhere(speech, segments, own, owner_forms, rules)):
        return f"'{weak[-1]}' 같은 약한 약속이라 담당자를 확정하지 않았습니다."
    firm = [m for (m, _), w in zip(endings, weak, strict=True) if w is None]

    subject = _promise_subject(speech, scope, own=bool(own), owner_forms=owner_forms, rules=rules)
    if subject and subject.kind == "group":
        return f"주어가 '{subject.text}'(기관·집단)라 개인 담당자로 확정하지 않았습니다."

    if not own and any(not s.speaker for s in segments) and _requested_of(speech, segments, owner_forms):
        return "회의록에 담당자에게 '요청'한 것으로 적혀 있어 배정으로 보지 않고 확정하지 않았습니다."
    if head and any(s.speaker for s in segments):
        if not own:
            return f"기관 대표({head})에게 한 요청이고, 본인이 맡겠다고 한 발언이 근거에 없어 확정하지 않았습니다."
        if not (firm and _says_directly(speech, firm[-1], rules)):
            return (
                f"기관 대표 발언자({head})의 약속에 '직접'이 없어 기관의 약속으로 보고 담당자를 확정하지 "
                "않았습니다(기관장급은 '제가 직접 ~하겠다'처럼 말할 때만 개인으로 확정)."
            )
    if not own and speech_only and firm and not (subject and subject.kind == "named"):
        speaker = next(s.speaker for s in segments if s.speaker)
        return f"근거는 다른 발언자({speaker})의 약속이라 담당자를 확정하지 않았습니다."
    return None
