"""요약·결정사항·미결 사항 문장을 사람이 쓴 것처럼 공손한 말투로 다듬는다 (추출이 끝난 뒤의 별도 단계).

추출 프롬프트(담당자·기한·할 일 판단)는 건드리지 않는다. 이 단계는 이미 검증된 문장만 받아 말투만 바꾸고,
결과가 원문과 어긋나면(개수, 숫자, 길이) 그 구역은 원래 문장을 그대로 쓴다. 모델이 없거나 실패해도 원문이 나간다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel

from ..llm.client import LLMUnavailable, get_client

logger = logging.getLogger(__name__)

SYSTEM = """너는 신입 공무원이 회의 내용을 동료들에게 Slack으로 공유하는 글을 다듬는 도우미다.
아래 세 목록의 문장을 사람이 직접 쓴 것처럼 자연스러운 공손한 말투(합쇼체)로 고쳐 쓴다.

규칙
1. 사실, 이름, 날짜, 숫자, 시각을 그대로 둔다. 원문에 없는 내용, 추측, 일반론을 넣지 않는다. 이름 뒤에 "씨", "님" 같은 호칭이나 직함을 새로 붙이지 않고, 원문에 적힌 호칭("주무관" 등)은 그대로 둔다.
2. "~했다", "~한다" 같은 평서체와 "~함" 같은 개조식을 "~했습니다", "~하기로 했습니다", "~할 예정입니다", "~논의했습니다"로 바꾼다.
3. decisions와 open_issues는 입력과 같은 개수, 같은 순서로 한 문장씩 돌려준다.
4. summary는 decisions와 의미가 겹치는 문장을 빼고, 남은 문장을 입력 순서대로 돌려준다. 모두 겹치면 빈 목록이어도 된다.
5. 이모지, 머리글, 번호를 쓰지 않는다. 문장 하나는 원문보다 훨씬 길어지지 않게 한다."""


class PolishOut(BaseModel):
    summary: list[str] = []
    decisions: list[str] = []
    open_issues: list[str] = []


@dataclass(frozen=True)
class Polished:
    summary: list[str]
    decisions: list[str]
    open_issues: list[str]
    changed: bool = False


_NUM = re.compile(r"\d+")


def _numbers(lines: list[str]) -> set[str]:
    return {m for line in lines for m in _NUM.findall(line)}


def _same_numbers(source: list[str], out: list[str]) -> bool:
    """새 숫자가 생기지 않고(날짜·시각 보존), 원문의 숫자가 사라지지도 않았는지 본다."""
    return _numbers(out) <= _numbers(source) and _numbers(source) <= _numbers(out)


def _lengths_ok(source: list[str], out: list[str]) -> bool:
    return all(0 < len(o.strip()) <= max(60, int(len(s) * 1.8)) for s, o in zip(source, out))


def polish_overview(summary: list[str], decisions: list[str], open_issues: list[str], *, client=None) -> Polished:
    original = Polished(summary, decisions, open_issues)
    if not (summary or decisions or open_issues):
        return original
    prompt = "\n".join(
        ["summary:", *(f"- {s}" for s in summary), "decisions:", *(f"- {d}" for d in decisions),
         "open_issues:", *(f"- {o}" for o in open_issues)]
    )
    try:
        out, _model = (client or get_client()).generate_structured(prompt, PolishOut, SYSTEM)
    except LLMUnavailable:
        return original
    except Exception:  # 다듬기는 선택 사항이다. 어떤 실패도 추출 결과를 막지 않는다.
        logger.warning("polish failed", exc_info=True)
        return original

    dec = [d.strip() for d in out.decisions]
    iss = [o.strip() for o in out.open_issues]
    summ = [s.strip() for s in out.summary if s.strip()]
    ok_dec = len(dec) == len(decisions) and _lengths_ok(decisions, dec) and _same_numbers(decisions, dec)
    ok_iss = len(iss) == len(open_issues) and _lengths_ok(open_issues, iss) and _same_numbers(open_issues, iss)
    # summary는 문장이 줄 수 있어 개수는 보지 않고, 숫자가 새로 생기지 않았는지만 확인한다 (원문 + 결정사항 안의 숫자까지 허용)
    ok_sum = len(summ) <= len(summary) and _numbers(summ) <= _numbers(summary) | _numbers(decisions)
    if not ok_sum:
        summ = summary
    result = Polished(
        summ if ok_sum else summary,
        dec if ok_dec else decisions,
        iss if ok_iss else open_issues,
    )
    return Polished(result.summary, result.decisions, result.open_issues,
                    changed=(result.summary, result.decisions, result.open_issues) != (summary, decisions, open_issues))
