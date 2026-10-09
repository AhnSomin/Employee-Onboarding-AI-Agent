"""Deterministic checks of a model answer against the retrieved source text (section 7.5).

What is checked (each check reports passed / failed / warning / not_applicable):
1. 근거 ID — every evidence id is a chunk retrieved or looked up for this question.
2. 근거 연결 — an answered reply has at least one point, and every point and
   condition cites at least one evidence id.
3. 조문 표기 — every "제n조(의m)" in the reply is the article of a cited chunk,
   or is referred to in a cited chunk's text. "제n항" with an article must
   exist in that article (warning when it cannot be told).
4. 수치·날짜 — every Arabic number with a unit (일, 시간, 개월, 년, 원, %, 회 …)
   and every date in a point or condition appears in the text of the chunks
   that point cites (whitespace ignored). For a cited table or annex chunk the
   bare number is enough, because table cells carry the unit in the header. Korean number words (하루, 열흘…)
   cannot be compared automatically and give a warning. A number that is not
   in the source but is in the user's own words this conversation (question,
   earlier question, stated conditions — e.g. "재직 12년") gives a warning,
   not a failure: repeating the user's situation is not inventing a figure.
5. 문서명 — every law or regulation name in the reply is the title of a cited
   chunk's document, or is named in a cited chunk's text.
6. 단서·예외 — a cited chunk with a proviso or exception that no condition
   cites gives a warning ("원문의 단서·예외 확인 필요").

What is not checked: whether the meaning of a condition or the scope of
application is interpreted correctly. That limit is shown on screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import AnswerPoint, ModelAnswer, RegChunk

CHECK_NAMES = ("근거 ID", "근거 연결", "조문 표기", "수치·날짜", "문서명", "단서·예외")
BLOCKING = CHECK_NAMES[:5]
ARTICLE_REF = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?(?:\s*제\s*(\d+)\s*항)?")
NUMBER_UNIT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(일|시간|개월|년|원|만원|%|퍼센트|회|분|세|주|배|명|개|차)")
DATE = re.compile(r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?|\d{1,2}\s*월\s*\d{1,2}\s*일")
KOREAN_NUMBER = re.compile(r"하루|이틀|사흘|나흘|닷새|엿새|이레|여드레|아흐레|열흘|보름|한 달|두 달|반나절")
QUOTED_LAW = re.compile(r"「([^」]+)」")
CIRCLED = {str(i + 1): chr(0x2460 + i) for i in range(20)}


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


def validate_answer(
    answer: ModelAnswer,
    retrieved: dict[str, RegChunk],
    doc_titles: dict[str, str],
    user_text: str = "",
) -> Validation:
    """`retrieved`: chunk id -> chunk for everything found this turn; `doc_titles`: doc id -> title;
    `user_text`: what the user said this conversation (questions and stated conditions)."""
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
    for kind, item in items:
        chunks = [retrieved[eid] for eid in item.evidence_ids if eid in retrieved]
        source = compact(" ".join(c.search_text for c in chunks))

        # 3. articles
        for match in ARTICLE_REF.finditer(item.text):
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

        # 4. numbers and dates. A table cell holds a bare number under a header such as "일수",
        # so for table and annex chunks the number alone (as a whole number) is enough.
        table_numbers = {n for c in chunks if c.kind in ("table", "annex") for n in re.findall(r"\d+", c.text)}
        for match in [*NUMBER_UNIT.finditer(item.text), *DATE.finditer(item.text)]:
            number_state = "passed" if number_state == "not_applicable" else number_state
            bare = match.re is NUMBER_UNIT and match.group(1) in table_numbers
            if compact(match.group(0)) in source or bare:
                continue
            if user_text and compact(match.group(0)) in compact(user_text):
                number_state = "warning" if number_state != "failed" else number_state
                warnings.append(f"'{match.group(0).strip()}'는 근거 원문이 아니라 질문에 나온 수치입니다.")
                continue
            number_state = "failed"
            failures.append(f"{kind} '{_short(item.text)}'의 '{match.group(0).strip()}'가 근거 원문에 없습니다.")
        if KOREAN_NUMBER.search(item.text):
            number_state = "warning" if number_state != "failed" else number_state
            warnings.append(f"한글 수사('{KOREAN_NUMBER.search(item.text).group(0)}')는 자동으로 비교하지 못했습니다.")

        # 5. document names
        cited_titles = {doc_titles.get(c.doc_id, "") for c in chunks}
        mentioned = {title for title in titles if title in item.text}
        mentioned |= {short_names[s] for s in short_names if s in item.text and short_names[s] not in mentioned}
        mentioned |= {m.group(1).strip() for m in QUOTED_LAW.finditer(item.text)}
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


def _short(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
