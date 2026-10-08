"""convert_assembly.py on a tiny synthetic workbook (no dataset content in the repo)."""

import importlib.util
import io
import sys
import zipfile
from xml.sax.saxutils import escape

import pytest

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.loader import load_meeting

HEADER = ["질의응답", "회의번호", "질의응답번호", "회의록구분", "대수", "회의구분", "위원회", "회수",
          "차수", "회의일자", "안건", "안건\t발언자", "의원ID", "ISNI", "발언순번", "발언내용"]
LETTERS = "ABCDEFGHIJKLMNOP"


def xlsx(rows: list[list[str]]) -> bytes:
    """Minimal workbook: the first header cell is a shared string, the rest inline strings."""
    cells = []
    for r, row in enumerate(rows, start=1):
        parts = []
        for c, value in enumerate(row):
            ref = f"{LETTERS[c]}{r}"
            if r == 1 and c == 0:
                parts.append(f'<c r="{ref}" t="s"><v>0</v></c>')
            else:
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        cells.append(f'<row r="{r}">{"".join(parts)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}"><si><t>{HEADER[0]}</t></si></sst>')
        book.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{"".join(cells)}</sheetData></worksheet>')
    return buffer.getvalue()


def row(qa, conference, order, speaker, text, agenda="1. 가상 안건", date="2002年11月2日(土)"):
    return [qa, conference, "0001", "국회", "16", "소위원회", "가상위원회", "제1회", "제2차", date, agenda,
            speaker, "", "", str(order), text]


@pytest.fixture(scope="module")
def convert():
    spec = importlib.util.spec_from_file_location("convert_assembly", REPO_ROOT / "scripts" / "convert_assembly.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["convert_assembly"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("convert_assembly", None)


def test_rows_are_read_and_header_checked(convert):
    records = convert.source_records(xlsx([HEADER, row("Q", "000001", 2, "가상 위원", "자료를 제출해 주십시오.")]))
    assert records[0]["speaker"] == "가상 위원" and records[0]["order"] == "2"
    with pytest.raises(ValueError):
        convert.source_records(xlsx([["다른", "형식"], ["a", "b"]]))


def test_pairs_join_in_speaking_order_without_duplicates(convert):
    first = convert.source_records(xlsx([HEADER, row("Q", "000001", 3, "가상 위원", "셋째 발언."), row("A", "000001", 4, "가상 차관", "넷째 발언.")]))
    second = convert.source_records(xlsx([HEADER, row("Q", "000001", 1, "가상 위원", "첫째   발언."), row("A", "000001", 4, "가상 차관", "넷째 발언.")]))
    groups = convert.group_agendas([("a.xlsx", r) for r in first] + [("b.xlsx", r) for r in second])
    group = groups[("000001", "1. 가상 안건")]
    text, truncated = convert.render(group)
    lines = text.splitlines()
    assert lines[0] == "회의명: 가상위원회 제1회 제2차 (소위원회)"
    assert lines[1] == "일시: 2002.11.02(토)"
    assert lines[5:] == ["가상 위원: 첫째 발언.", "가상 위원: 셋째 발언.", "가상 차관: 넷째 발언."]
    assert not truncated and group.members == ["a.xlsx", "b.xlsx"]
    loaded = load_meeting(text=text)  # the meeting loader reads the header
    assert (loaded.title_suggestion, str(loaded.meeting_date)) == ("가상위원회 제1회 제2차 (소위원회)", "2002-11-02")


def test_long_agendas_are_cut_at_utterance_boundaries(convert):
    records = convert.source_records(xlsx([HEADER] + [row("Q", "000002", n, "가상 위원", "가" * 40) for n in range(5)]))
    group = convert.group_agendas([("c.xlsx", r) for r in records])[("000002", "1. 가상 안건")]
    text, truncated = convert.render(group, max_chars=100)
    assert truncated and text.count("가상 위원:") == 2 and text.rstrip().endswith("(이하 생략)")


def test_pick_prefers_agendas_with_requests_or_commitments(convert):
    plain = [row("Q", "000003", n, "가상 위원", "의견을 말씀드립니다. " * 60) for n in range(3)]
    tasks = [row("Q", "000004", n, "가상 위원", "자료를 다음 주까지 제출해 주십시오. " * 40) for n in range(3)]
    records = convert.source_records(xlsx([HEADER] + plain + tasks))
    groups = convert.group_agendas([("d.xlsx", r) for r in records])
    assert convert.pick(groups, 6000).meta["conference"] == "000004"


def test_dates_and_output_location(convert, tmp_path, capsys):
    assert convert.format_date("1997년7월23일(수)") == "1997.07.23(수)"
    assert convert.format_date("2020년10월07일") == "2020.10.07"
    assert convert.main(["--dataset", str(tmp_path), "--out", str(tmp_path / "x")]) == 1
    assert "찾지 못했습니다" in capsys.readouterr().out
