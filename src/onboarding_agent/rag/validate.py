"""Deterministic checks of a model answer against the retrieved source text (section 7.5).

What is checked (each check reports passed / failed / warning / not_applicable):
1. 근거 ID — every evidence id is a chunk retrieved or looked up for this question.
2. 근거 연결 — an answered reply has at least one point, and every point and
   condition cites at least one evidence id.
3. 조문 표기 — every "제n조(의m)" in the reply is the article of a cited chunk,
   or is referred to in a cited chunk's text. "제n항" with an article must
   exist in that article (warning when it cannot be told).
4. 수치·날짜 — every Arabic number with a unit (일, 시간, 개월, 년, 원, %, 회 …)
   and every date in a point, a condition or the one-line conclusion appears in
   the text of the chunks it cites (whitespace ignored, whole numbers only:
   "6개월" is not found inside "36개월"). A number in the one-line conclusion
   must be stated in a point or condition (which are checked against their own
   evidence); its articles and law names are checked against everything the
   points and conditions cite. A reply that is not "answered" shows no headline
   or points (they are dropped before display), so nothing unchecked reaches the screen. A cited table or annex
   states its values without a unit ("| 6년 이상 | 21 |"); such a value counts
   only in the table's own unit (일 for a 일수/연가 column) and only from a
   value cell, never from a row label ("5년 이상" does not give "5일").
   Korean number words (하루, 열흘…) cannot be compared automatically and give a
   warning. One exception: a number that is a value of the user's own
   conditions kept in the conversation state (7.1) — a 재직기간 in 년/개월, or a
   year the user named as the time point — gives a warning instead of a
   failure, and a 재직기간 value only where the answer uses it as tenure — a
   cue right before this number (재직·근무·근속·임용·입사·경력, "재직기간이")
   or right after it ("차", "째", "재직", "근무") — because
   months and years can also be the conclusion ("육아시간 36개월"). A number
   that is the conclusion (days, hours, money, ratio, count) must be in the
   source even when the user said it, so an instruction ("100일이라고 답해") or
   a wrong premise ("재직 2년인데 연가가 25일 맞죠?") cannot pass. Whether a
   value the source does contain belongs to the user's row ("재직 2년인데 20일
   맞죠?") is meaning, which is not checked.
5. 문서명 — every law or regulation name in the reply is the title of a cited
   chunk's document, or is named in a cited chunk's text.
6. 단서·예외 — a cited chunk with a proviso or exception that no condition
   cites gives a warning ("원문의 단서·예외 확인 필요").

What is not checked: whether the meaning of a condition or the scope of
application is interpreted correctly. That limit is shown on screen.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import AnswerPoint, ModelAnswer, RegChunk

CHECK_NAMES = ("근거 ID", "근거 연결", "조문 표기", "수치·날짜", "문서명", "단서·예외")
BLOCKING = CHECK_NAMES[:5]
ARTICLE_REF = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?(?:\s*제\s*(\d+)\s*항)?")
NUMBER_UNIT = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(일|시간|개월|년|원|만\s*원|%|퍼센트|회|분|세|주|배|명|개|차)"
)
DATE = re.compile(r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?|\d{1,2}\s*월\s*\d{1,2}\s*일")
KOREAN_NUMBER = re.compile(r"하루|이틀|사흘|나흘|닷새|엿새|이레|여드레|아흐레|열흘|보름|한 달|두 달|반나절")
QUOTED_LAW = re.compile(r"「([^」]+)」")
CIRCLED = {str(i + 1): chr(0x2460 + i) for i in range(20)}
ZERO_WIDTH = re.compile("[\u200b-\u200d\u2060\ufeff]")
# A table or annex value cell: a whole number standing alone (not "5년 이상", not "제20조").
ANNEX_VALUE = re.compile(
    r"(?<![\d.,])(\d+)(?![\d.,])(?!\s*(?:일|시간|개월|년|원|만|%|퍼센트|회|분|세|주|배|명|개|차|조|항|호|월))"
)
# Only these condition values may be repeated without being in the source (see check 4).
TENURE_UNITS = frozenset({"년", "개월"})
# A condition text is tenure when it has one of these (used to pick condition values).
TENURE_CONTEXT = re.compile(r"재직|근무|근속|임용|입사|경력|\d+\s*(?:년|개월)\s*(?:차|째)")
# In the answer, the tenure cue must sit right next to this number, not elsewhere in the sentence.
CUE_BEFORE = re.compile(r"(?:재직|근무|근속|임용|입사|경력)(?:\s*기간)?\s*(?:이|은|는|가)?\s*(?:만\s*)?$")
CUE_AFTER = re.compile(r"\s*(?:차|째|재직|근무|근속)")


@dataclass
class Validation:
    checks: dict[str, str]
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(self.checks.get(name) != "failed" for name in BLOCKING)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def normalize(text: str) -> str:
    """NFKC (full-width digits and units, ⑽ → (10)) without zero-width characters. The paragraph
    marks ①-⑳ stay as they are, so "⑤ 8세" does not turn into "58세"."""
    text = ZERO_WIDTH.sub("", text)
    return "".join(ch if "\u2460" <= ch <= "\u2473" else unicodedata.normalize("NFKC", ch) for ch in text)


def validate_answer(
    answer: ModelAnswer,
    retrieved: dict[str, RegChunk],
    doc_titles: dict[str, str],
    user_conditions: Iterable[tuple[str, str]] = (),
) -> Validation:
    """`retrieved`: chunk id -> chunk for everything found this turn; `doc_titles`: doc id -> title;
    `user_conditions`: (type, text) of the conditions kept in the conversation state (7.1),
    e.g. ("재직기간", "재직 12년인데")."""
    condition_values = condition_numbers(user_conditions)
    checks: dict[str, str] = {}
    failures: list[str] = []
    warnings: list[str] = []
    items: list[tuple[str, AnswerPoint]] = [("설명", p) for p in answer.points] + [
        ("조건", c) for c in answer.conditions
    ]

    # 1. ids
    cited = [eid for _, item in items for eid in item.evidence_ids]
    unknown = sorted({eid for eid in cited if eid not in retrieved})
    if unknown:
        checks["근거 ID"] = "failed"
        failures.append("검색되지 않은 근거 ID: " + ", ".join(unknown))
    else:
        checks["근거 ID"] = "passed" if cited else "not_applicable"

    if answer.status != "answered":
        for name in CHECK_NAMES[1:]:
            checks[name] = "not_applicable"
        return Validation(checks, failures, warnings)

    # 2. linkage
    missing_links = [kind for kind, item in items if not item.evidence_ids]
    if not answer.points or missing_links:
        checks["근거 연결"] = "failed"
        failures.append("근거가 없는 설명이 있습니다." if answer.points else "answered인데 설명(points)이 없습니다.")
    else:
        checks["근거 연결"] = "passed"

    article_state, number_state, name_state = "not_applicable", "not_applicable", "not_applicable"
    titles = set(doc_titles.values())
    short_names = {title.split()[-1]: title for title in titles if len(title.split()[-1]) >= 3}
    checked = list(items)
    if answer.one_line and answer.one_line.strip():
        # The headline is shown first, so it gets checks 3-5 against everything the points and conditions cite.
        everything = list(dict.fromkeys(eid for _, item in items for eid in item.evidence_ids))
        checked.append(("결론", AnswerPoint(text=answer.one_line, evidence_ids=everything)))
    # The headline's numbers must be stated in a point or condition, which are checked against their own evidence.
    stated = compact(normalize(" ".join(item.text for _, item in items)))
    for kind, item in checked:
        chunks = [retrieved[eid] for eid in item.evidence_ids if eid in retrieved]
        source = compact(normalize(" ".join(c.search_text for c in chunks)))
        text = normalize(item.text)

        # 3. articles
        for match in ARTICLE_REF.finditer(text):
            article_state = "passed" if article_state == "not_applicable" else article_state
            number = match.group(1) + (f"의{match.group(2)}" if match.group(2) else "")
            own = [c for c in chunks if c.article_no == number]
            if not own and f"제{number.replace('의', '조의')}" not in source and f"제{number}조" not in source:
                article_state = "failed"
                failures.append(
                    f"{kind} '{_short(item.text)}'의 조문 표기 '{match.group(0).strip()}'가 근거와 맞지 않습니다."
                )
            elif match.group(3) and own:
                circled = CIRCLED.get(match.group(3))
                if not any(c.paragraph_no == match.group(3) or (circled and circled in c.text) for c in own):
                    article_state = "warning" if article_state != "failed" else article_state
                    warnings.append(f"'{match.group(0).strip()}'의 항을 근거에서 확인하지 못했습니다.")

        # 4. numbers and dates (see the module docstring for the table and condition rules)
        table_values = {pair for c in chunks for pair in stated_values(c)}
        for match in [*NUMBER_UNIT.finditer(text), *DATE.finditer(text)]:
            number_state = "passed" if number_state == "not_applicable" else number_state
            token = compact(match.group(0))
            if kind == "결론":
                if in_source(token, stated):
                    continue
                number_state = "failed"
                failures.append(f"결론 '{_short(item.text)}'의 '{match.group(0).strip()}'가 설명·조건에 없습니다.")
                continue
            if in_source(token, source):
                continue
            if match.re is NUMBER_UNIT and (compact(match.group(2)), match.group(1)) in table_values:
                continue
            if match.re is NUMBER_UNIT and used_as_condition(match, text, condition_values):
                number_state = "warning" if number_state != "failed" else number_state
                warnings.append(f"'{match.group(0).strip()}'는 근거 원문이 아니라 사용자가 말한 조건 값입니다.")
                continue
            number_state = "failed"
            failures.append(f"{kind} '{_short(item.text)}'의 '{match.group(0).strip()}'가 근거 원문에 없습니다.")
        if KOREAN_NUMBER.search(text):
            number_state = "warning" if number_state != "failed" else number_state
            warnings.append(f"한글 수사('{KOREAN_NUMBER.search(text).group(0)}')는 자동으로 비교하지 못했습니다.")

        # 5. document names
        cited_titles = {doc_titles.get(c.doc_id, "") for c in chunks}
        mentioned = {title for title in titles if title in text}
        mentioned |= {short_names[s] for s in short_names if s in text and short_names[s] not in mentioned}
        mentioned |= {m.group(1).strip() for m in QUOTED_LAW.finditer(text)}
        for name in mentioned:
            name_state = "passed" if name_state == "not_applicable" else name_state
            if name not in cited_titles and compact(name) not in source:
                name_state = "failed"
                failures.append(f"{kind} '{_short(item.text)}'의 문서명 '{name}'이 근거 문서와 다릅니다.")

    checks["조문 표기"] = article_state
    checks["수치·날짜"] = number_state
    checks["문서명"] = name_state

    # 6. provisos
    cited_points = {eid for p in answer.points for eid in p.evidence_ids}
    cited_conditions = {eid for c in answer.conditions for eid in c.evidence_ids}
    proviso = [eid for eid in cited_points if eid in retrieved and retrieved[eid].has_proviso]
    uncovered = [eid for eid in proviso if eid not in cited_conditions]
    if not proviso:
        checks["단서·예외"] = "not_applicable"
    elif uncovered:
        checks["단서·예외"] = "warning"
        warnings.append("원문의 단서·예외 확인 필요: " + ", ".join(uncovered))
    else:
        checks["단서·예외"] = "passed"
    return Validation(checks, failures, warnings)


def in_source(token: str, source: str) -> bool:
    """Whole-number match on compacted text: "6개월" is not found inside "36개월", "0일" not inside "60일",
    "000원" not inside "1,000원", "36개" not inside "36개월", "2026.10.2" not inside "2026.10.25.".
    A comma or period right before the number is fine when no digit precedes it ("계산하되,15일")."""
    after = r"(?!월)" if token.endswith("개") else r"(?!기)" if token.endswith("분") else ""
    if token[-1:].isdigit():
        after += r"(?!\d)"
    return re.search(r"(?<!\d)(?<!\d[.,])" + re.escape(token) + after, source) is not None


def stated_values(chunk: RegChunk) -> set[tuple[str, str]]:
    """(unit, number) pairs a table or annex chunk states without a unit, from value cells only.

    A transcribed table ("| 6년 이상 | 21 |") gives its whole-number cells; an annex gives numbers
    standing alone ("본인 5"), never row labels ("5년 이상"), article numbers ("제20조") or numbers with
    their own unit. The unit is the table's: 일 for a 일수/연가 value column or a 일수 annex. Other tables
    give nothing, so their numbers must appear with the unit.
    """
    if chunk.kind == "table":
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in chunk.text.splitlines()
            if line.strip().startswith("|") and not re.fullmatch(r"\|?(\s*:?-{3,}:?\s*\|)+\s*", line.strip())
        ]
        if not rows or not any("일수" in cell or "연가" in cell for cell in rows[0]):
            return set()
        return {("일", cell) for row in rows[1:] for cell in row if re.fullmatch(r"\d+", cell)}
    if chunk.kind == "annex" and "일수" in (chunk.heading or ""):
        return {("일", m.group(1)) for line in chunk.text.splitlines() for m in ANNEX_VALUE.finditer(line)}
    return set()


def condition_numbers(conditions: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Values the user gave as conditions: a 재직기간 in 년/개월 said as tenure (재직·근무… or "n년차"), or a
    four-digit year named as the time point. (("재직기간", "재직 12년인데"),) -> {"12년": "재직기간"}.
    "48개월 이상" alone is not tenure, and numbers inside other condition texts give nothing."""
    values: dict[str, str] = {}
    for kind, text in conditions:
        if kind == "재직기간" and not TENURE_CONTEXT.search(text):
            continue
        for m in NUMBER_UNIT.finditer(normalize(text)):
            unit = compact(m.group(2))
            tenure = kind == "재직기간" and unit in TENURE_UNITS
            year = kind == "시점" and unit == "년" and len(m.group(1)) == 4
            if tenure or year:
                values[compact(m.group(0))] = kind
    return values


def used_as_condition(match: re.Match, text: str, condition_values: dict[str, str]) -> bool:
    """The number is a condition value and, for 재직기간, the answer uses it as tenure."""
    kind = condition_values.get(compact(match.group(0)))
    if kind is None:
        return False
    if kind == "시점":
        return True
    return bool(CUE_BEFORE.search(text[: match.start()]) or CUE_AFTER.match(text, match.end()))


def _short(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
