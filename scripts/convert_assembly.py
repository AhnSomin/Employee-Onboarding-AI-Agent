"""Turn National Assembly minutes (public dataset 023) into agenda-level text excerpts.

Run with:
    uv run python scripts/convert_assembly.py --dataset "<023 dataset folder>" [--count 5] [--max-chars 6000]

The dataset stays where it is and is only read (zip files, never extracted
in place). Excerpts and their manifest are written only under
data/external/, which git ignores: never commit the source, conversions, or
labels made from them (see docs/DECISIONS.md). Excerpts are for extraction
and evaluation only; their file names start with "assembly_" so the
executor never sends them to Calendar or Slack.

Each source xlsx holds one question/answer pair. Pairs sharing a meeting
number and agenda are joined in speaking order into one agenda transcript.
Excerpts come from different meetings, one meeting type at a time,
preferring agenda groups with requests or commitments ("제출", "검토하겠습니다",
"~까지") so extraction has something to find.

Length: MEETING_MAX_CHARS (100,000) is only the hard limit. Extraction time
grows with the input and the quotes the model copies back, so excerpts are
capped at 6,000 characters (cut at utterance boundaries). That keeps about
90% of agenda groups whole (their 90th percentile is 1.4k-4.9k characters
by meeting type) while staying a size a reviewer can label by hand.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from onboarding_agent.config import REPO_ROOT

DEFAULT_OUT = REPO_ROOT / "data" / "external" / "assembly"
MAX_CHARS = 6000
MIN_CHARS = 1500
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
# Column letters of the source sheets (the header row is checked before use).
COLUMNS = {
    "A": "qa", "B": "conference", "C": "qa_number", "E": "generation", "F": "meeting_type",
    "G": "committee", "H": "session", "I": "sitting", "J": "date", "K": "agenda",
    "L": "speaker", "O": "order", "P": "text",
}
EXPECTED_HEADER = {"A": "질의응답", "B": "회의번호", "K": "안건", "O": "발언순번", "P": "발언내용"}
ACTION_MARKERS = re.compile(r"제출|보고(?:해|드리)|검토하겠|조치하겠|하겠습니다|해 주시기 바랍니다|해 주십시오|까지")
WEEKDAYS = dict(zip("月火水木金土日", "월화수목금토일", strict=True))


def read_rows(xlsx_bytes: bytes) -> list[dict[str, str]]:
    """Rows of the first sheet as {column letter: text}, read from the xlsx XML."""
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")) for si in root.findall("m:si", NS)]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.iter(f"{{{NS['m']}}}row"):
        values: dict[str, str] = {}
        for cell in row.findall("m:c", NS):
            column = re.match(r"[A-Z]+", cell.get("r", "")).group()
            value, inline = cell.find("m:v", NS), cell.find("m:is", NS)
            if cell.get("t") == "s" and value is not None:
                values[column] = shared[int(value.text)]
            elif inline is not None:
                values[column] = "".join(t.text or "" for t in inline.iter(f"{{{NS['m']}}}t"))
            elif value is not None:
                values[column] = value.text or ""
        rows.append(values)
    return rows


def source_records(xlsx_bytes: bytes) -> list[dict[str, str]]:
    rows = read_rows(xlsx_bytes)
    if not rows:
        return []
    header = rows[0]
    for column, title in EXPECTED_HEADER.items():
        if title not in header.get(column, ""):
            raise ValueError(f"unexpected source layout: column {column} is {header.get(column)!r}")
    return [{name: row.get(column, "").strip() for column, name in COLUMNS.items()} for row in rows[1:]]


def format_date(raw: str) -> str:
    """'2002年11月2日(土)' or '1997년7월23일(수)' -> '2002.11.02(토)'."""
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})[^(]*(?:\((.)\))?", raw)
    if not match:
        return raw
    year, month, day, weekday = match.groups()
    label = f"{int(year)}.{int(month):02d}.{int(day):02d}"
    return f"{label}({WEEKDAYS.get(weekday, weekday)})" if weekday else label


@dataclass
class AgendaGroup:
    meta: dict[str, str]
    utterances: dict[int, tuple[str, str]] = field(default_factory=dict)  # order -> (speaker, text)
    members: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(text) for _, text in self.utterances.values())

    @property
    def action_markers(self) -> int:
        return sum(len(ACTION_MARKERS.findall(text)) for _, text in self.utterances.values())


def group_agendas(records: list[tuple[str, dict[str, str]]]) -> dict[tuple[str, str], AgendaGroup]:
    groups: dict[tuple[str, str], AgendaGroup] = {}
    for member, record in records:
        key = (record["conference"], record["agenda"])
        group = groups.setdefault(key, AgendaGroup(meta=record))
        order = int(record["order"]) if record["order"].isdigit() else len(group.utterances)
        group.utterances.setdefault(order, (record["speaker"], " ".join(record["text"].split())))
        if member not in group.members:
            group.members.append(member)
    return groups


def render(group: AgendaGroup, max_chars: int = MAX_CHARS) -> tuple[str, bool]:
    """Agenda transcript in a format the meeting loader reads (회의명, 일시). Returns (text, truncated)."""
    meta = group.meta
    lines, used, truncated = [], 0, False
    for _, (speaker, text) in sorted(group.utterances.items()):
        if used + len(text) > max_chars:
            if not lines:  # a single long utterance: keep its beginning
                lines.append(f"{speaker}: {text[:max_chars]}…(중략)")
            truncated = True
            break
        lines.append(f"{speaker}: {text}")
        used += len(text)
    header = [
        f"회의명: {meta['committee']} {meta['session']} {meta['sitting']} ({meta['meeting_type']})",
        f"일시: {format_date(meta['date'])}",
        f"안건: {meta['agenda']}",
        "※ 출처: 국회 회의록 기반 지식검색 데이터(공개 데이터셋)의 질의·답변 발언을 안건 단위로 이어 붙인 발췌입니다.",
        "",
    ]
    footer = ["(이하 생략)"] if truncated else []
    return "\n".join(header + lines + footer) + "\n", truncated


def load_groups(zip_path: Path) -> dict[tuple[str, str], AgendaGroup]:
    records = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".xlsx"):
                continue
            name = info.filename
            try:
                name = name.encode("cp437").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            records.extend((Path(name).name, r) for r in source_records(archive.read(info)))
    return group_agendas(records)


def pick(groups: dict[tuple[str, str], AgendaGroup], max_chars: int) -> AgendaGroup | None:
    """The agenda group most likely to contain tasks, within the length window."""
    candidates = [g for g in groups.values() if MIN_CHARS <= g.chars <= max_chars and len(g.utterances) >= 2]
    if not candidates:
        candidates = [g for g in groups.values() if g.chars >= MIN_CHARS]
    if not candidates:
        return None
    return max(candidates, key=lambda g: (g.action_markers, len(g.utterances), g.meta["conference"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="국회 회의록 데이터셋에서 안건 단위 발췌를 만듭니다.")
    parser.add_argument("--dataset", type=Path, required=True, help="'023.국회 회의록 기반 지식검색 데이터' 폴더")
    parser.add_argument("--split", default="Validation", choices=["Validation", "Training"])
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--max-chars", type=int, default=MAX_CHARS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    prefix = "VS_" if args.split == "Validation" else "TS_"
    sources = sorted(args.dataset.rglob(f"{prefix}*.zip"))
    if not sources:
        print(f"원천데이터 zip({prefix}*.zip)을 찾지 못했습니다: {args.dataset}")
        return 1
    out = args.out.resolve()
    if REPO_ROOT / "data" / "external" not in [out, *out.parents]:
        print("발췌는 data/external/ 아래에만 만들 수 있습니다 (git 제외 경로).")
        return 1
    out.mkdir(parents=True, exist_ok=True)

    manifest = []
    for zip_path in sources[: args.count]:
        group = pick(load_groups(zip_path), args.max_chars)
        if group is None:
            continue
        text, truncated = render(group, args.max_chars)
        meta = group.meta
        file_name = f"assembly_{meta['meeting_type']}_{meta['conference']}.txt"
        (out / file_name).write_text(text, encoding="utf-8")
        manifest.append(
            {
                "file": file_name,
                "source_zip": zip_path.name,
                "conference": meta["conference"],
                "meeting_type": meta["meeting_type"],
                "committee": meta["committee"],
                "date": format_date(meta["date"]),
                "agenda": meta["agenda"],
                "chars": len(text),
                "utterances": len(group.utterances),
                "truncated": truncated,
                "action_markers": group.action_markers,
                "source_members": group.members,
            }
        )
        print(f"- {file_name}: {len(text):,}자, 발언 {len(group.utterances)}개{' (잘림)' if truncated else ''}")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"발췌 {len(manifest)}개를 {out}에 만들었습니다 (git 제외).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
