"""Turn parsed statutes into SourceDoc metadata and retrieval chunks.

One article is one chunk. An article is split only when its length estimate
is far above the target (SPLIT_OVER): first at paragraphs (①②…), then at
items (1. 2. …). A piece never starts with a proviso ("다만", "단,"), and every
piece repeats the "[법령명] 제n조(제목) 제m항" header in its embed_text.
Deleted paragraphs ("⑦ 삭제<…>") are not kept.

`text` keeps the extracted lines as they are, including the marker for a
table printed as an image. `search_text` joins the print lines (a break may
fall on a space or inside a word; see join_lines), leaves the image marker
out, drops amendment notes and collapses whitespace. Only spacing changes:
numbers, negations, provisos and exceptions are never changed.

Token counts are estimates. `estimate_tokens` uses the character count as an
upper bound (a Korean character is at most about one token); it is never
reported as a measured token count.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .models import RegChunk, SourceDoc
from .parse_law import IMAGE_MARKER, Article, Line, ParsedLaw, article_label

SPLIT_OVER = 1200  # estimated tokens (upper bound); the target is about 300-900
PARAGRAPH = re.compile(r"^\s*([①-⑳])")
DELETED_PARAGRAPH = re.compile(r"^\s*[①-⑳]\s*삭제\s*(?:<[^>]*>)?\s*$")
ITEM = re.compile(r"^\s*(\d+)\.\s")
PROVISO = re.compile(r"다만|단,|그러하지 아니하다|예외로 한다|제외한다|불구하고")
AMENDMENT = re.compile(
    r"<(?:개정|신설|삭제|본조신설|전문개정|제목개정|타법개정|단서 생략)[^>]*>"
    r"|\[(?:전문개정|본조신설|제목개정|종전|시행일|제\d+조[^\]]*에서 이동)[^\]]*\]"
)
FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}
REF = re.compile(
    r"(?:「(?P<quoted>[^」]+)」\s*(?:\([^)]*\)\s*)?"
    r"|(?P<prefix>같은 법|같은 영|같은 규칙|이 법|이 영|이 규칙|법|영|규칙)\s?)?"
    r"제(?P<no>\d+)조(?:의(?P<branch>\d+))?"
)
LIST_GLUE = re.compile(r"^(?:\s|,|및|또는|ㆍ|부터|까지|제\d+항|제\d+호|[가-하]목)*$")
ALIAS = re.compile(r"「([^」]+)」\s*\(이하\s*[“\"]([^”\"]+)[”\"]\s*(?:이)?라\s*한다\)")
ANNEX_REF = re.compile(r"(별표|별지)\s*(?:제\s*)?(\d+(?:의\d+)?)")
PARAGRAPH_NO = {chr(0x2460 + i): str(i + 1) for i in range(20)}


def estimate_tokens(text: str) -> int:
    return len(text)


def normalize(text: str) -> str:
    """Full-width characters folded, amendment notes dropped, whitespace collapsed."""
    text = AMENDMENT.sub(" ", text.translate(FULLWIDTH))
    return re.sub(r"\s+", " ", text).strip()


def join_lines(lines: list[str], keep_marker: bool = False) -> str:
    """Join print lines into running text.

    A print line breaks either at a space or inside a word, so each join is
    decided on its own: after "." or "," a space (dates, lists); next to the
    middle dot (ㆍ) none; otherwise Kiwi's spacing model looks only at the last
    word before the break and the first word after it. Spacing elsewhere is
    left as printed. The image-table marker is kept only when asked.
    """
    out = ""
    for line in (line.strip() for line in lines):
        if not line or (line == IMAGE_MARKER and not keep_marker):
            continue
        if not out:
            out = line
        elif line == IMAGE_MARKER or out.endswith(IMAGE_MARKER):
            out = f"{out} {line}"
        else:
            out = out + (" " if _space_at_break(out, line) else "") + line
    return out


def _space_at_break(before: str, after: str) -> bool:
    if before.endswith((".", ",")):
        return True
    if before.endswith("ㆍ") or after.startswith("ㆍ"):
        return False
    left = before.rsplit(" ", 1)[-1]
    right = after.split(" ", 1)[0]
    return _kiwi_space_between(left, right)


def _kiwi_space_between(left: str, right: str) -> bool:
    from ..retrieval.tokenize import _kiwi, _lock

    with _lock:
        spaced = _kiwi().space(left + right, reset_whitespace=False)
    seen = 0
    for index, char in enumerate(spaced):
        if char != " ":
            seen += 1
            if seen == len(left):
                return index + 1 < len(spaced) and spaced[index + 1] == " "
    return True


def flow_text(text: str, keep_marker: bool = False) -> str:
    """A chunk's printed text as running text (see join_lines), amendment notes dropped."""
    return normalize(join_lines(text.split("\n"), keep_marker=keep_marker))


def doc_id_for(parsed: ParsedLaw, sha256: str) -> str:
    """Deterministic id from the printed version label, e.g. 대통령령 제36728호 -> dec36728."""
    label = parsed.version_label or ""
    number = re.search(r"제\s*(\d+)호", label)
    prefixes = (
        ("대통령령", "dec"),
        ("총리령", "pmo"),
        ("부령", "min"),
        ("법률", "act"),
        ("훈령", "ins"),
        ("예규", "rul"),
        ("고시", "ntc"),
    )
    for word, prefix in prefixes:
        if word in label and number:
            return f"{prefix}{number.group(1)}"
    return "doc" + hashlib.sha1((parsed.title + sha256).encode("utf-8")).hexdigest()[:10]


def source_doc(
    parsed: ParsedLaw,
    *,
    sha256: str,
    source_uri: str | None,
    collected_at: datetime,
    source_kind: str = "local_file",
    external_send_allowed: bool = True,
) -> SourceDoc:
    label = parsed.version_label or ""
    if any(word in label for word in ("법률", "대통령령", "총리령", "부령")):
        doc_type = "law"
    elif any(word in label for word in ("훈령", "예규", "고시")):
        doc_type = "admin_rule"
    else:
        doc_type = "guide"
    unverified = ["applies_to"]  # stated per article (적용범위), not as one document value
    for name in ("effective_date", "promulgation_date", "version_label"):
        if getattr(parsed, name) is None:
            unverified.append(name)
    return SourceDoc(
        doc_id=doc_id_for(parsed, sha256),
        doc_title=parsed.title,
        doc_type=doc_type,
        version_label=parsed.version_label,
        effective_date=parsed.effective_date,
        promulgation_date=parsed.promulgation_date,
        applies_to=None,
        source_kind=source_kind,
        source_uri=source_uri,
        source_sha256=sha256,
        collected_at=collected_at,
        external_send_allowed=external_send_allowed,
        unverified=unverified,
    )


def chunk_id(doc_id: str, article_no: str, part: int | None = None) -> str:
    base = f"{doc_id}:a{article_no.replace('의', '-')}"
    return f"{base}:p{part}" if part else base


def chunk_law(
    parsed: ParsedLaw, doc: SourceDoc, known_docs: dict[str, str] | None = None
) -> tuple[list[RegChunk], list[dict]]:
    """Chunks for every live article, and the paragraphs left out (deleted ones).

    `known_docs` maps the titles of other indexed documents to their doc ids so
    references to them become chunk ids.
    """
    known = {doc.doc_title: doc.doc_id, **(known_docs or {})}
    articles = {a.article_no for a in parsed.articles if not a.deleted}
    all_lines = [line.text for a in parsed.articles for line in a.lines]
    aliases = {alias: title for title, alias in ALIAS.findall(normalize(join_lines(all_lines)))}
    chunks: list[RegChunk] = []
    dropped: list[dict] = []
    for article in parsed.articles:
        if article.deleted:
            continue
        pieces = _split(article)
        kept = [(number, lines) for number, lines in pieces if not _deleted_paragraph(lines)]
        dropped += [
            {"kind": "삭제 항", "detail": f"{doc.doc_title} {article_label(article.article_no)} 제{number}항"}
            for number, lines in pieces
            if _deleted_paragraph(lines)
        ]
        for part, (paragraph_no, lines) in enumerate(kept, start=1):
            text = "\n".join(line.text.rstrip() for line in lines if line.text.strip())
            search = normalize(join_lines([line.text for line in lines]))
            header = f"[{doc.doc_title}] {article_label(article.article_no)}"
            header += f"({article.title})" if article.title else ""
            if paragraph_no:
                header += f" 제{paragraph_no}항"
            chunks.append(
                RegChunk(
                    chunk_id=chunk_id(doc.doc_id, article.article_no, part if len(kept) > 1 else None),
                    doc_id=doc.doc_id,
                    article_no=article.article_no,
                    article_title=article.title,
                    paragraph_no=paragraph_no,
                    section_path=article.section_path,
                    text=text,
                    search_text=search,
                    embed_text=f"{header}\n{search}",
                    location=_location(lines),
                    refs=_refs(search, doc, article, articles, known, aliases),
                    has_proviso=bool(PROVISO.search(search)),
                )
            )
    return chunks, dropped


def link_refs(chunks: list[RegChunk]) -> list[RegChunk]:
    """Point article-level refs at real chunk ids (the first piece of a split article).

    A ref to an article that is not indexed (deleted, or another version) keeps
    a readable label instead of a dangling id.
    """
    ids = {c.chunk_id for c in chunks}
    first_piece: dict[str, str] = {}
    for c in chunks:
        first_piece.setdefault(c.chunk_id.split(":p")[0], c.chunk_id)
    linked = []
    for c in chunks:
        own = c.chunk_id.split(":p")[0]
        refs = []
        for ref in c.refs:
            if ":a" in ref and not ref.startswith("「") and ref not in ids:
                ref = first_piece.get(ref, f"{ref} (색인에 없음)")
            if ref.split(":p")[0] != own:
                refs.append(ref)
        linked.append(c.model_copy(update={"refs": list(dict.fromkeys(refs))}))
    return linked


def _deleted_paragraph(lines: list[Line]) -> bool:
    text = " ".join(line.text.strip() for line in lines if line.text.strip())
    return bool(DELETED_PARAGRAPH.match(text))


def _split(article: Article) -> list[tuple[str | None, list[Line]]]:
    """[(paragraph number or None, lines)] — the whole article unless it is far too long."""
    lines = [line for line in article.lines if line.text.strip()]
    if estimate_tokens("\n".join(line.text for line in lines)) <= SPLIT_OVER:
        return [(None, lines)]
    paragraphs: list[tuple[str | None, list[Line]]] = []
    for line in lines:
        match = PARAGRAPH.match(line.text)
        if match or not paragraphs:
            paragraphs.append((PARAGRAPH_NO.get(match.group(1)) if match else None, [line]))
        else:
            paragraphs[-1][1].append(line)
    if len(paragraphs) == 1:
        paragraphs = [(None, lines)]
    pieces: list[tuple[str | None, list[Line]]] = []
    for number, para_lines in paragraphs:
        pieces.extend((number, group) for group in _split_items(para_lines))
    merged: list[tuple[str | None, list[Line]]] = []
    for number, group in pieces:
        if merged and group and group[0].text.strip().startswith(("다만", "단,")):
            merged[-1] = (merged[-1][0], merged[-1][1] + group)  # a proviso stays with what it qualifies
        else:
            merged.append((number, group))
    return merged


def _split_items(lines: list[Line]) -> list[list[Line]]:
    if estimate_tokens("\n".join(line.text for line in lines)) <= SPLIT_OVER:
        return [lines]
    groups: list[list[Line]] = [[]]
    for line in lines:
        size = estimate_tokens("\n".join(x.text for x in groups[-1] + [line]))
        if ITEM.match(line.text) and groups[-1] and size > SPLIT_OVER:
            groups.append([])
        groups[-1].append(line)
    return groups


def _location(lines: list[Line]) -> dict:
    if lines and lines[0].pdf_page is not None:
        printed = [line.printed_page for line in lines if line.printed_page is not None]
        return {
            "pdf_page": lines[0].pdf_page,
            "pdf_page_end": lines[-1].pdf_page,
            "printed_page": printed[0] if printed else None,
            "printed_page_end": printed[-1] if printed else None,
        }
    numbers = [line.line_no for line in lines if line.line_no is not None]
    return {"lines": [numbers[0], numbers[-1]] if numbers else None}


def _refs(
    text: str,
    doc: SourceDoc,
    article: Article,
    articles: set[str],
    known: dict[str, str],
    aliases: dict[str, str],
) -> list[str]:
    """Article and annex references, as chunk ids when the target is indexed, else as wording."""
    refs: list[str] = []
    last_title: str | None = None  # the most recent 「law」, for "같은 법/영"
    previous: tuple[int, str | None] | None = None  # (end of last match, its target title)
    for match in REF.finditer(text):
        no = match.group("no") + (f"의{match.group('branch')}" if match.group("branch") else "")
        prefix = match.group("prefix") or ""
        if match.group("quoted"):
            title = last_title = match.group("quoted").strip()
        elif prefix.startswith("같은"):
            title = last_title
        elif prefix.startswith("이 "):
            title = doc.doc_title
        elif prefix:
            title = aliases.get(prefix)
            if title is None:
                refs.append(f"{prefix} {article_label(no)}")
                previous = (match.end(), None)
                continue
        elif previous and LIST_GLUE.match(text[previous[0] : match.start()]):
            title = previous[1] or doc.doc_title  # "제2조제3항, 제5조제3항 및 …" continues the list
        else:
            title = doc.doc_title
        previous = (match.end(), title)
        target = known.get(title or "")
        if target == doc.doc_id and (no == article.article_no or no not in articles):
            continue
        refs.append(chunk_id(target, no) if target else f"「{title}」 {article_label(no)}")
    for match in ANNEX_REF.finditer(text):
        refs.append(f"{match.group(1)} {match.group(2)} (미색인)")
    return list(dict.fromkeys(refs))
