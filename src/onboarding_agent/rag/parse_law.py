"""Parse statute text (법제처 PDF prints or plain text) into chapters and articles.

- Page furniture is dropped: the "법제처 n 국가법령정보센터" line printed at the
  top of every page (n is the printed page number, kept as location) and the
  running title line under it.
- The header "[시행 YYYY. M. D.] [대통령령 제N호, YYYY. M. D., 일부개정]" gives the
  effective date, version label and promulgation date. Nothing else is
  inferred: the file name and the collection date are never used as dates.
- Chapters and sections (장·절) become the section path of the articles below
  them, not chunks. Supplementary provisions (부칙) and everything after them
  are excluded and listed. Deleted articles ("제12조 삭제") are listed, not kept.
- Article numbers must increase; a heading-like line that would go backwards
  is body text (a wrapped reference). Gaps (제14조 then 제16조) become warnings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

FURNITURE = re.compile(r"^\s*법제처\s+(\d+)\s+국가법령정보센터\s*$")
EFFECTIVE = re.compile(r"\[시행\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\]")
VERSION = re.compile(
    r"\[((?:법률|대통령령|총리령|[가-힣]+부령|부령|훈령|예규|고시)\s*제\s*\d+호),\s*"
    r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?,\s*([^\]]+)\]"
)
ARTICLE = re.compile(r"^제(\d+)조(?:의(\d+))?\(((?:[^()]|\([^()]*\))*)\)\s?(.*)$")
DELETED = re.compile(r"^제(\d+)조(?:의(\d+))?\s*(?:\([^)]*\)\s*)?삭제(?:\s*<[^>]*>)?\s*$")
CHAPTER = re.compile(r"^제(\d+장(?:의\d+)?)\s+(.+?)(?:\s*<[^>]*>)?$")
SECTION = re.compile(r"^제(\d+절(?:의\d+)?)\s+(.+?)(?:\s*<[^>]*>)?$")
ADDENDA = re.compile(r"^부\s*칙(?:\s|<|$)")
ANNEX = re.compile(r"\[(별표|별지)\s*(?:제\s*)?(\d+(?:의\d+)?)(?:호)?(?:\s*서식)?\]")
GARBLED = re.compile(r"[�-]")


IMAGE_MARKER = "[표·그림(이미지) — 텍스트로 추출되지 않아 미색인]"


@dataclass(frozen=True)
class Line:
    text: str
    pdf_page: int | None = None  # 1-based page in the file
    printed_page: int | None = None  # page number printed by 법제처
    line_no: int | None = None  # 1-based line in a text file
    image: bool = False  # marks where a picture (e.g. a table printed as an image) sits


@dataclass
class Article:
    article_no: str  # "15", "15의2"
    title: str | None
    section_path: str | None
    lines: list[Line] = field(default_factory=list)
    deleted: bool = False

    @property
    def key(self) -> tuple[int, int]:
        return article_key(self.article_no)


@dataclass
class ParsedLaw:
    title: str
    effective_date: date | None
    promulgation_date: date | None
    version_label: str | None
    revision_kind: str | None
    articles: list[Article]
    excluded: list[dict]
    warnings: list[str]
    pages: int | None = None


def article_key(article_no: str) -> tuple[int, int]:
    main, _, branch = article_no.partition("의")
    return int(main), int(branch or 0)


def article_label(article_no: str) -> str:
    return f"제{article_no.replace('의', '조의')}" + ("" if "의" in article_no else "조")


def read_pdf_lines(path: Path) -> tuple[list[Line], int]:
    """Text lines of a PDF with page furniture removed, and the page count.

    Lines come with their vertical position. On a page that holds images, an
    unusually large gap between two text lines is where a picture sits (for
    example a table printed as an image); an IMAGE_MARKER line is put there.
    Images are not OCR'd.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [_page_lines(page) for page in reader.pages]
    return lines_from_pages(pages, [_has_images(page) for page in reader.pages]), len(pages)


def lines_from_pages(pages: list[list[tuple[str, float | None]]], has_images: list[bool]) -> list[Line]:
    """Drop page furniture and mark image gaps, given each page's (text, y) lines."""
    title = _first_title([[text for text, _ in page] for page in pages])
    lines: list[Line] = []
    for index, (page, images) in enumerate(zip(pages, has_images, strict=True), start=1):
        printed: int | None = None
        body: list[tuple[str, float | None]] = []
        for text, y in page:
            match = FURNITURE.match(text)
            if match and printed is None:
                printed = int(match.group(1))
                continue
            if title and text.strip() == title and not body and index > 1:
                continue  # running title at the top of every page after the first
            body.append((text, y))
        gaps = _image_gaps(body) if images else set()
        for position, (text, _) in enumerate(body):
            lines.append(Line(text, index, printed))
            if position in gaps:
                lines.append(Line(IMAGE_MARKER, index, printed, image=True))
    return lines


def _page_lines(page) -> list[tuple[str, float | None]]:
    """(line text, y position) in reading order, from pypdf's text visitor."""
    fragments: list[tuple[str, float]] = []

    def visit(text, cm, tm, font, size):  # noqa: ARG001 - pypdf visitor signature
        if text:
            fragments.append((text, tm[4] * cm[1] + tm[5] * cm[3] + cm[5]))

    page.extract_text(visitor_text=visit)
    lines: list[tuple[str, float | None]] = []
    current, y = "", None
    for text, position in fragments:
        parts = text.split("\n")
        for number, part in enumerate(parts):
            if part.strip() and y is None:
                y = position
            current += part
            if number < len(parts) - 1:
                lines.append((current, y))
                current, y = "", None
    if current.strip():
        lines.append((current, y))
    return lines


def _has_images(page) -> bool:
    try:
        return bool(page.images)
    except Exception:  # an unreadable image object still means there is one
        return True


def _image_gaps(body: list[tuple[str, float | None]]) -> set[int]:
    """Indexes of lines followed by a gap far larger than the usual line spacing."""
    ys = [(i, y) for i, (text, y) in enumerate(body) if y is not None and text.strip()]
    steps = [a[1] - b[1] for a, b in zip(ys, ys[1:], strict=False) if a[1] > b[1]]
    if not steps:
        return set()
    usual = sorted(steps)[len(steps) // 2]
    return {a[0] for a, b in zip(ys, ys[1:], strict=False) if a[1] - b[1] > max(4 * usual, 60)}


def _first_title(pages: list[list[str]]) -> str | None:
    if not pages:
        return None
    return next((line.strip() for line in pages[0] if line.strip() and not FURNITURE.match(line)), None)


def read_text_lines(text: str) -> list[Line]:
    return [Line(line, line_no=number) for number, line in enumerate(text.split("\n"), start=1)]


def parse_pdf(path: Path) -> ParsedLaw:
    lines, pages = read_pdf_lines(path)
    parsed = parse_lines(lines)
    parsed.pages = pages
    return parsed


def parse_lines(lines: list[Line]) -> ParsedLaw:
    warnings: list[str] = []
    excluded: list[dict] = []
    if any(GARBLED.search(line.text) for line in lines):
        warnings.append("글자 깨짐 의심 문자(대체 문자·사용자 정의 영역)가 있습니다.")
    if not any(line.text.strip() for line in lines):
        return ParsedLaw("", None, None, None, None, [], [], ["텍스트를 추출하지 못했습니다(빈 추출)."])

    title = next(line.text.strip() for line in lines if line.text.strip())
    head = "\n".join(line.text for line in lines[:12])
    effective = _date(EFFECTIVE.search(head))
    version = VERSION.search(head)
    version_label = re.sub(r"\s+", " ", version.group(1)) if version else None
    promulgation = date(int(version.group(2)), int(version.group(3)), int(version.group(4))) if version else None
    revision_kind = version.group(5).strip() if version else None

    articles: list[Article] = []
    chapter: str | None = None
    section: str | None = None
    current: Article | None = None
    preamble = 0
    last_key = (0, 0)
    for index, line in enumerate(lines):
        stripped = line.text.strip()
        if not stripped:
            continue
        if ADDENDA.match(stripped):
            rest = lines[index:]
            excluded.append({"kind": "부칙", "detail": stripped[:80], "chars": sum(len(x.text) for x in rest)})
            for annex in sorted({f"{m.group(1)} {m.group(2)}" for x in rest for m in ANNEX.finditer(x.text)}):
                excluded.append({"kind": "별표·서식", "detail": f"{annex} (부칙 뒤, 미색인)"})
            break
        heading = _chapter_or_section(line.text, stripped)
        if heading:
            kind, label = heading
            if kind == "장":
                chapter, section = label, None
            else:
                section = label
            continue
        deleted = DELETED.match(stripped)
        match = ARTICLE.match(stripped)
        number = deleted or match
        if number:
            no = number.group(1) + (f"의{number.group(2)}" if number.group(2) else "")
            key = article_key(no)
            if key > last_key:
                if key[1] == 0 and key[0] > last_key[0] + 1:
                    warnings.append(f"조문 번호 건너뜀: {article_label(_no(last_key))} 다음 {article_label(no)}")
                last_key = key
                path = " > ".join(part for part in (chapter, section) if part) or None
                if deleted:
                    current = None
                    articles.append(Article(no, None, path, [line], deleted=True))
                    excluded.append({"kind": "삭제 조문", "detail": article_label(no)})
                    continue
                current = Article(no, match.group(3).strip() or None, path, [line])
                articles.append(current)
                continue
        if current is None:
            preamble += 1  # title, effective-date header, ministry contact lines
            continue
        if line.image:
            excluded.append(
                {
                    "kind": "이미지 표·그림",
                    "detail": f"{article_label(current.article_no)} (파일 {line.pdf_page}쪽, OCR하지 않음)",
                }
            )
        current.lines.append(line)
    if preamble:
        excluded.append({"kind": "머리말", "detail": f"제목·시행 정보·소관 부처 연락처 {preamble}줄(본문 아님)"})
    if not [a for a in articles if not a.deleted]:
        warnings.append("조문을 찾지 못했습니다.")
    return ParsedLaw(title, effective, promulgation, version_label, revision_kind, articles, excluded, warnings)


def _no(key: tuple[int, int]) -> str:
    return f"{key[0]}" + (f"의{key[1]}" if key[1] else "")


def _chapter_or_section(raw: str, stripped: str) -> tuple[str, str] | None:
    # Headings are short and indented in the 법제처 print; body lines that
    # happen to start with "제2장" are references inside a sentence. A trailing
    # amendment note ("<개정 2011. 7. 4.>") does not count towards the length.
    bare = re.sub(r"\s*<[^>]*>\s*$", "", stripped)
    if len(bare) > 40 or bare.endswith(("다.", "다")):
        return None
    for kind, pattern in (("장", CHAPTER), ("절", SECTION)):
        match = pattern.match(bare)
        if match and (raw[:1].isspace() or len(bare) <= 25):
            return kind, f"제{match.group(1)} {match.group(2).strip()}"
    return None


def _date(match: re.Match | None) -> date | None:
    if not match:
        return None
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
