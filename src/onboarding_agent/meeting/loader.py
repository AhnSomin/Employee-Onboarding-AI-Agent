"""Meeting text input: decoding, size check, title and meeting-date suggestions (spec 8.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePath

from ..config import get_settings
from .dates import find_absolute_date

ENCODINGS = ("utf-8", "utf-8-sig", "cp949")
SUPPORTED_SUFFIXES = (".txt", ".md")

_DATE_LABEL = re.compile(r"(?:일시|일자|날짜|회의\s*일시?|개최\s*일시?|date)\s*[:：]?", re.IGNORECASE)
_TITLE_LABEL = re.compile(r"^(?:회의\s*명|회의\s*제목|제목|회의)\s*[:：]\s*(?P<title>.+)$")
_DECORATION = re.compile(r"^[#■□◆◇●○▶▷\-*\s]+|[■□◆◇●○◀◁\s]+$")
_BRACKETED = re.compile(r"^[\[【(](?P<inner>[^\]】)]+)[\]】)]")
_DATE_TEXT = re.compile(
    r"(?:20\d{2}\s*(?:[-./]|년)\s*\d{1,2}\s*(?:[-./]|월)\s*\d{1,2}\s*일?|\d{1,2}\s*월\s*\d{1,2}\s*일)"
    r"\s*(?:\(\s*[월화수목금토일]\s*\))?"
)


class MeetingInputError(ValueError):
    """The input cannot be processed. The message is user-facing Korean."""


@dataclass(frozen=True)
class LoadedMeeting:
    text: str
    encoding: str | None
    title_suggestion: str
    meeting_date: date
    meeting_date_detected: bool  # False: today was used and the user must confirm
    source_filename: str | None


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode uploaded bytes trying utf-8, utf-8-sig, then cp949 (Windows Korean)."""
    for encoding in ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding == "utf-8" and text.startswith("﻿"):
            return text[1:], "utf-8-sig"
        return text, encoding
    raise MeetingInputError("파일 인코딩을 읽을 수 없습니다. UTF-8이나 CP949로 저장된 텍스트 파일을 올려 주세요.")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _non_empty_lines(text: str, limit: int) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip()][:limit]


def suggest_title(text: str, filename: str | None = None) -> str:
    lines = _non_empty_lines(text, 10)
    for line in lines:
        labeled = _TITLE_LABEL.match(line)
        if labeled:
            return labeled["title"].strip()[:80]
    if lines:
        first = _DECORATION.sub("", lines[0])
        bracketed = _BRACKETED.match(first)
        if bracketed:
            # "[시설 점검 회의] 2026.03.02" -> inner; "[녹취 전사] 멘토링 회의" -> rest
            inner = bracketed["inner"].strip()
            rest = _DATE_TEXT.sub("", first[bracketed.end() :]).strip(" -·|/")
            first = inner if ("회의" in inner or not rest) else rest
        else:
            first = _DATE_TEXT.sub("", first).strip(" -·|/")
        if first and len(first) <= 60 and not _DATE_LABEL.match(first):
            return first
    if filename:
        stem = PurePath(filename).stem.replace("_", " ").strip()
        if stem:
            return stem
    return "제목 없는 회의"


def detect_meeting_date(text: str, today: date) -> date | None:
    """Meeting date from a labeled line ("일시: ..."), else from the first lines."""
    lines = _non_empty_lines(text, 30)
    for line in lines:
        label = _DATE_LABEL.search(line)
        if label:
            found = find_absolute_date(line[label.end() :], today.year)
            if found:
                return found
    for line in lines[:10]:
        found = find_absolute_date(line, today.year)
        if found:
            return found
    return None


def load_meeting(
    *,
    data: bytes | None = None,
    text: str | None = None,
    filename: str | None = None,
    today: date | None = None,
    max_chars: int | None = None,
) -> LoadedMeeting:
    """Prepare uploaded bytes or pasted text for extraction."""
    settings = get_settings()
    if filename and not filename.lower().endswith(SUPPORTED_SUFFIXES):
        raise MeetingInputError("지원하는 파일 형식은 .txt와 .md입니다.")
    if data is not None:
        text, encoding = decode_bytes(data)
    elif text is not None:
        encoding = None
    else:
        raise MeetingInputError("회의록 파일을 올리거나 내용을 붙여 넣어 주세요.")

    text = normalize_newlines(text).strip("\n")
    if not text.strip():
        raise MeetingInputError("회의록 내용이 비어 있습니다.")
    limit = max_chars if max_chars is not None else settings.meeting_max_chars
    if len(text) > limit:
        raise MeetingInputError(
            f"회의록이 너무 깁니다 ({len(text):,}자). {limit:,}자 이하로 나누어 올려 주세요."
        )

    today = today or datetime.now(settings.tz).date()
    detected = detect_meeting_date(text, today)
    return LoadedMeeting(
        text=text,
        encoding=encoding,
        title_suggestion=suggest_title(text, filename),
        meeting_date=detected or today,
        meeting_date_detected=detected is not None,
        source_filename=filename,
    )
