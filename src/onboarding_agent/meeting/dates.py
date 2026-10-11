"""Resolve Korean due-date expressions against the meeting date (spec 8.3).

Weeks start on Monday. A result is confirmed only when the expression names
exactly one day that is not before the meeting date; anything vaguer stays
unconfirmed, with a note telling the reviewer why.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, time, timedelta

WEEKDAY_CHARS = "월화수목금토일"


@dataclass(frozen=True)
class DueResolution:
    due_date: date | None = None
    due_time: time | None = None
    confirmed: bool = False
    notes: tuple[str, ...] = ()


def weekday_label(day: date) -> str:
    return WEEKDAY_CHARS[day.weekday()]


def format_due(day: date, at: time | None = None) -> str:
    """'10/16(금)' or '10/16(금) 15:00'."""
    label = f"{day.month}/{day.day}({weekday_label(day)})"
    return f"{label} {at:%H:%M}" if at else label


# --- date rules -------------------------------------------------------------


@dataclass
class _Hit:
    day: date | None
    certain: bool = True
    notes: list[str] = field(default_factory=list)


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _invalid() -> _Hit:
    return _Hit(None, False, ["존재하지 않는 날짜입니다."])


def _full_date(m: re.Match[str], base: date) -> _Hit:
    day = _make_date(int(m["y"]), int(m["m"]), int(m["d"]))
    return _Hit(day) if day else _invalid()


def _year_less(month: int, day_of_month: int, base: date) -> _Hit:
    day = _make_date(base.year, month, day_of_month)
    if day is None:
        return _invalid()
    if day < base:
        next_year = _make_date(base.year + 1, month, day_of_month)
        if next_year is None:
            return _invalid()
        return _Hit(
            next_year,
            False,
            [f"연도가 없는 날짜가 회의 날짜보다 앞서 {next_year.year}년으로 보았습니다."],
        )
    return _Hit(day)


def _month_day(m: re.Match[str], base: date) -> _Hit:
    return _year_less(int(m["m"]), int(m["d"]), base)


_WEEK_OFFSETS = {"이번주": 0, "금주": 0, "다음주": 1, "차주": 1, "담주": 1, "다다음주": 2}


def _week_weekday(m: re.Match[str], base: date) -> _Hit:
    offset = _WEEK_OFFSETS["".join(m["week"].split())]
    monday = base - timedelta(days=base.weekday()) + timedelta(weeks=offset)
    return _Hit(monday + timedelta(days=WEEKDAY_CHARS.index(m["wd"])))


_DAY_WORD_OFFSETS = {"오늘": 0, "금일": 0, "내일": 1, "명일": 1, "모레": 2, "내일모레": 2, "글피": 3}


def _day_word(m: re.Match[str], base: date) -> _Hit:
    return _Hit(base + timedelta(days=_DAY_WORD_OFFSETS["".join(m[0].split())]))


def _bare_weekday(m: re.Match[str], base: date) -> _Hit:
    # Nearest day strictly after the meeting date.
    delta = (WEEKDAY_CHARS.index(m["wd"]) - base.weekday()) % 7 or 7
    return _Hit(base + timedelta(days=delta))


def _span(m: re.Match[str], base: date) -> _Hit:
    if m["week1"]:
        days = 7
    else:
        days = int(m["n"]) * (7 if m["unit"] == "주" else 1)
    return _Hit(base + timedelta(days=days))


def _last_day_of_month(year: int, month: int) -> date:
    first_of_next = date(year + month // 12, month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


def _month_end_day(m: re.Match[str], base: date) -> _Hit:
    if m["next"]:
        year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
    else:
        year, month = base.year, base.month
    return _Hit(_last_day_of_month(year, month))


def _next_month_day(m: re.Match[str], base: date) -> _Hit:
    year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
    day = _make_date(year, month, int(m["d"]))
    return _Hit(day) if day else _invalid()


def _day_of_month(m: re.Match[str], base: date) -> _Hit:
    day = _make_date(base.year, base.month, int(m["d"]))
    return _Hit(day) if day else _invalid()


_FULL_DATE = re.compile(
    r"(?P<y>20\d{2})\s*(?:[-./]|년)\s*(?P<m>\d{1,2})\s*(?:[-./]|월)\s*(?P<d>\d{1,2})(?:\s*일)?"
)
_MONTH_DAY = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일")
_SLASH_DATE = re.compile(r"(?<![\d./])(?P<m>\d{1,2})\s*[/.]\s*(?P<d>\d{1,2})(?![\d./])")
_WEEK_WEEKDAY = re.compile(
    r"(?P<week>다다음\s*주|다음\s*주|이번\s*주|금주|차주|담주)\s*(?P<wd>[월화수목금토일])요일"
)
_DAY_WORD = re.compile(r"내일\s*모레|오늘|금일|내일|명일|모레|글피")
_BARE_WEEKDAY = re.compile(r"(?P<wd>[월화수목금토일])요일")
_SPAN = re.compile(
    r"(?:(?P<n>\d{1,3})\s*(?P<unit>일|주)|(?P<week1>일주일))\s*(?:이내|안에|내|안|후|뒤)"
)
_MONTH_END_DAY = re.compile(r"(?:(?P<next>다음\s*달)|이번\s*달|이달)?\s*말일")
_NEXT_MONTH_DAY = re.compile(r"다음\s*달\s*(?P<d>\d{1,2})\s*일")
_DAY_OF_MONTH = re.compile(r"(?<!\d)(?P<d>\d{1,2})\s*일(?!\s*(?:이내|안|내|후|뒤|간|동안))")

# (pattern, resolver, may be followed by a weekday annotation such as "(금)")
_DATE_RULES: list[tuple[re.Pattern[str], Callable[[re.Match[str], date], _Hit], bool]] = [
    (_FULL_DATE, _full_date, True),
    (_MONTH_DAY, _month_day, True),
    (_SLASH_DATE, lambda m, base: _year_less(int(m["m"]), int(m["d"]), base), True),
    (_WEEK_WEEKDAY, _week_weekday, False),
    (_DAY_WORD, _day_word, False),
    (_SPAN, _span, False),
    (_MONTH_END_DAY, _month_end_day, False),
    (_NEXT_MONTH_DAY, _next_month_day, True),
    (_BARE_WEEKDAY, _bare_weekday, False),
    (_DAY_OF_MONTH, _day_of_month, True),
]

_ANNOTATION = re.compile(
    r"\s*(?:\(\s*(?P<wd1>[월화수목금토일])(?:요일)?\s*\)|(?P<wd2>[월화수목금토일])요일)"
)
_RECURRING = re.compile(r"매주|매일|매월|매달|격주")
_VAGUE = re.compile(
    r"이번\s*주\s*중|다음\s*주\s*중|주중|월\s*말|이달\s*말|이번\s*달\s*말|연말|월\s*초|다음\s*달|"
    r"이달\s*중|이번\s*달\s*중|중순|상순|하순|조만간|가능한\s*(?:한\s*)?빨리|가급적\s*빨리|최대한\s*빨리|"
    r"빠른\s*시일|asap|다음\s*회의|추후|미정|수시|상시|분기|상반기|하반기|연내|올해\s*안",
    re.IGNORECASE,
)

# --- time rules -------------------------------------------------------------

_AMPM_TIME = re.compile(
    r"(?P<ampm>오전|오후|새벽|아침|낮|저녁|밤)\s*(?P<h>\d{1,2})\s*시(?!간)"
    r"(?:\s*(?P<min>\d{1,2})\s*분|\s*(?P<half>반))?"
)
_CLOCK_TIME = re.compile(r"(?<!\d)(?P<h>\d{1,2}):(?P<min>\d{2})(?!\d)")
_BARE_HOUR = re.compile(
    r"(?<!\d)(?P<h>\d{1,2})\s*시(?!간)(?:\s*(?P<min>\d{1,2})\s*분|\s*(?P<half>반))?"
)
_TIME_RULES = (_AMPM_TIME, _CLOCK_TIME, _BARE_HOUR)


def _clock(hour: int, minute: int) -> time | None:
    return time(hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _minutes(m: re.Match[str]) -> int:
    if m["half"]:
        return 30
    return int(m["min"]) if m["min"] else 0


def _ampm_hour(ampm: str, hour: int) -> int:
    if ampm in ("오전", "새벽", "아침"):
        return 0 if hour == 12 else hour
    if ampm == "낮":
        return hour + 12 if hour <= 6 else hour
    return hour if hour == 12 else hour + 12  # 오후, 저녁, 밤


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _resolve_time(work: str) -> tuple[time | None, bool, list[str]]:
    """Return (time, certain, notes) from text whose date parts are masked."""
    found: list[time] = []
    notes: list[str] = []
    certain = True
    spans: list[tuple[int, int]] = []

    for m in _AMPM_TIME.finditer(work):
        at = _clock(_ampm_hour(m["ampm"], int(m["h"])), _minutes(m))
        if at:
            found.append(at)
        spans.append(m.span())
    work = _mask(work, spans)
    spans = []
    for m in _CLOCK_TIME.finditer(work):
        at = _clock(int(m["h"]), int(m["min"]))
        if at:
            found.append(at)
        spans.append(m.span())
    work = _mask(work, spans)
    for m in _BARE_HOUR.finditer(work):
        hour = int(m["h"])
        if 13 <= hour <= 23:
            at = _clock(hour, _minutes(m))
            if at:
                found.append(at)
        else:
            certain = False
            notes.append("오전·오후 표시가 없어 시각을 비워 두었습니다.")

    distinct = sorted(set(found))
    if len(distinct) > 1:
        return None, False, [*notes, "시각이 둘 이상이라 비워 두었습니다."]
    return (distinct[0] if distinct else None), certain, notes


def resolve_due(text: str | None, meeting_date: date) -> DueResolution:
    """Resolve a due expression (e.g. "다음 주 금요일 오후 3시까지") against the meeting date."""
    if text is None or not text.strip():
        return DueResolution(notes=("기한 표현이 없습니다.",))
    work = " ".join(text.split())
    if _RECURRING.search(work):
        return DueResolution(notes=("반복 일정이라 기한을 하루로 정할 수 없습니다.",))

    hits: list[_Hit] = []
    for pattern, resolver, takes_annotation in _DATE_RULES:
        spans: list[tuple[int, int]] = []
        for m in pattern.finditer(work):
            hit = resolver(m, meeting_date)
            spans.append(m.span())
            if takes_annotation:
                annotation = _ANNOTATION.match(work, m.end())
                if annotation:
                    spans.append(annotation.span())
                    wd = annotation["wd1"] or annotation["wd2"]
                    if hit.day and weekday_label(hit.day) != wd:
                        hit.certain = False
                        hit.notes.append(
                            f"요일({wd})이 날짜와 맞지 않습니다. {format_due(hit.day)}입니다."
                        )
            hits.append(hit)
        work = _mask(work, spans)

    if not hits:
        if _VAGUE.search(text):
            return DueResolution(notes=("일 단위로 특정할 수 없는 기한입니다.",))
        return DueResolution(notes=("기한 표현을 해석하지 못했습니다.",))

    notes = [note for hit in hits for note in hit.notes]
    days = sorted({hit.day for hit in hits if hit.day})
    if len(days) != 1:
        if len(days) > 1:
            notes.append("날짜가 둘 이상이라 하나로 정할 수 없습니다.")
        return DueResolution(notes=tuple(notes))

    day = days[0]
    certain = all(hit.certain for hit in hits)
    if day < meeting_date:
        certain = False
        notes.append("회의 날짜보다 앞선 기한입니다.")
    at, time_certain, time_notes = _resolve_time(work)
    notes.extend(time_notes)
    return DueResolution(day, at, certain and time_certain, tuple(notes))


_DUE_TAIL = re.compile(r"\s*(?:\(\s*[월화수목금토일](?:요일)?\s*\))?\s*(?:까지|이내|안에|내로)?")


def find_due_text(sentence: str) -> str | None:
    """The due expression inside a sentence, for rule-based extraction.

    Starts at the earliest date or vague-deadline expression, joins date and
    time expressions that follow right after it, and keeps a trailing "까지".
    """
    date_spans = sorted(
        [m.span() for pattern, _, _ in _DATE_RULES for m in pattern.finditer(sentence)]
        + [m.span() for m in _VAGUE.finditer(sentence)]
    )
    if not date_spans:
        return None
    start, end = date_spans[0]
    following = sorted(
        date_spans[1:] + [m.span() for p in _TIME_RULES for m in p.finditer(sentence)]
    )
    for span_start, span_end in following:
        tail = _DUE_TAIL.match(sentence, end)
        reach = tail.end() if tail else end
        if start <= span_start <= reach + 1:
            end = max(end, span_end)
    tail = _DUE_TAIL.match(sentence, end)
    if tail:
        end = tail.end()
    return sentence[start:end].strip() or None


def find_absolute_date(text: str, default_year: int) -> date | None:
    """First explicit calendar date in `text` (used to detect the meeting date)."""
    full = _FULL_DATE.search(text)
    if full:
        return _make_date(int(full["y"]), int(full["m"]), int(full["d"]))
    month_day = _MONTH_DAY.search(text)
    if month_day:
        return _make_date(default_year, int(month_day["m"]), int(month_day["d"]))
    return None
