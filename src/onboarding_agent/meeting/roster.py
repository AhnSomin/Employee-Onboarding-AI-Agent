"""Demo roster and name matching (spec 8.3, 8.7).

The roster is never shown to the LLM. Code maps the name written in the
minutes ("김민준", "김 주무관", "민준 님") to a member; when several members
fit, the owner stays unconfirmed and the candidates are listed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

# Longest first so "부이사관" is stripped before "이사관".
TITLES = (
    "특별자치도지사", "특별자치시장", "특별시장", "광역시장",
    "소위원장", "부이사관", "이사관", "서기관", "사무관", "주무관", "연구관", "연구사", "비서관",
    "위원장", "부총리", "대변인", "이사장", "본부장", "후보자", "도지사", "구청장", "교육감",
    "부시장", "부지사", "부군수", "부의장",
    "과장", "팀장", "계장", "국장", "실장", "부장", "차장", "주사", "서기", "대리", "주임",
    "위원", "의원", "장관", "차관", "총리", "청장", "처장", "원장", "총장", "수석", "간사", "대사", "단장", "사장",
    "시장", "군수", "의장", "반장", "팀원",
)
_NAME = re.compile(r"[가-힣]{2,4}")
HONORIFICS = ("님", "씨")
TWO_CHAR_SURNAMES = ("남궁", "제갈", "선우", "독고", "황보", "사공", "서문")
# A name ending with one of these refers to a group, not a person.
GROUP_SUFFIXES = (
    "팀", "과", "실", "국", "부서", "센터", "본부", "위원회", "담당관", "담당자", "관계자",
    "전원", "모두", "다같이", "각자", "TF",
)


class RosterError(ValueError):
    """The roster file cannot be read. The message is user-facing Korean."""


class Member(BaseModel):
    name: str
    aliases: list[str] = []
    team: str | None = None
    slack_user_id: str | None = None


@dataclass(frozen=True)
class RosterMatch:
    member: Member | None
    candidates: tuple[Member, ...] = ()


def compact(text: str) -> str:
    return "".join(text.split())


def strip_titles(raw: str) -> str:
    """'김민준 주무관님' -> '김민준', '김 주무관' -> '김'."""
    name = compact(raw)
    changed = True
    while changed and name:
        changed = False
        for suffix in (*HONORIFICS, *TITLES):
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                changed = True
    return name


def is_group_reference(raw: str) -> bool:
    name = compact(raw)
    collective = name.startswith(("우리", "저희"))  # "저희 부처", "우리 위원회"
    return collective or any(name.endswith(suffix) for suffix in GROUP_SUFFIXES) or "에서" in name


def _is_title(token: str) -> bool:
    """'위원', '부총리겸가상재정부장관', '가상재판소장후보자' — but not '김민준주무관'."""
    if token in TITLES:
        return True
    return any(token.endswith(t) and len(token) - len(t) > 4 for t in TITLES)


def person_name(raw: str) -> str:
    """The person in an owner label: '한가람 위원' -> '한가람', '가상부제1차관 오세린' -> '오세린',
    '김 주무관' -> '김'. Titles may come before or after the name, as in Korean minutes."""
    tokens = raw.split()
    while tokens and _is_title(tokens[-1]):
        tokens.pop()
    if not tokens:
        return ""
    if len(tokens) >= 2 and _NAME.fullmatch(tokens[-1]):
        return tokens[-1]
    return strip_titles(" ".join(tokens))


def surname(name: str) -> str:
    return name[:2] if name[:2] in TWO_CHAR_SURNAMES else name[:1]


class Roster(BaseModel):
    members: list[Member] = []

    def match(self, raw: str) -> RosterMatch:
        """Match a name as written in the minutes to roster members."""
        key = compact(raw)
        if not key:
            return RosterMatch(None)
        exact = [
            m for m in self.members if key == compact(m.name) or key in map(compact, m.aliases)
        ]
        if exact:
            return _result(exact)

        core = strip_titles(raw)
        if not core:
            return RosterMatch(None)
        by_core = [
            m for m in self.members if core == compact(m.name) or core in map(strip_titles, m.aliases)
        ]
        if by_core:
            return _result(by_core)
        if len(core) == 1 or core in TWO_CHAR_SURNAMES:  # surname with a title, e.g. "박 주무관"
            return _result([m for m in self.members if surname(m.name) == core])
        given = [m for m in self.members if m.name[len(surname(m.name)) :] == core]
        if given:
            return _result(given)
        # "인사팀 김민준 주무관": try the last words on their own.
        words = raw.split()
        if len(words) > 1:
            return self.match(" ".join(words[1:]))
        return RosterMatch(None)


def _result(members: list[Member]) -> RosterMatch:
    unique = list({m.name: m for m in members}.values())
    if len(unique) == 1:
        return RosterMatch(unique[0], (unique[0],))
    return RosterMatch(None, tuple(unique))


def load_roster(path: Path) -> Roster:
    """Read the roster YAML. A missing file gives an empty roster."""
    if not path.is_file():
        return Roster()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Roster.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise RosterError(f"명단 파일 형식을 확인하세요: {path.name} ({type(exc).__name__})") from None
