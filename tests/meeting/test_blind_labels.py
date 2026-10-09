"""blind_labels.py: plain YAML labels become the evaluation JSONL (fictional minutes only)."""

import importlib.util
import json
import sys

import pytest

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.evaluation import load_gold

MINUTES = """회의명: 가상 팀 주간 회의
일시: 2026.10.08(목) 10:00
참석자: 김민준 주무관, 이서연 사무관

- 김민준 주무관: 제가 금요일까지 교육 자료를 정리하겠습니다.
- 이서연 사무관: 장비 점검은 다음에 다시 이야기하죠.
"""

LABELS = """회의날짜: 2026-10-08
항목:
  - 할일: 교육 자료 정리
    낱말: [교육 자료]
    담당자: 김민준
    기한: 2026-10-09
    필수: 예
  - 할일: 장비 점검
    낱말: 장비
    담당자:
    기한:
    필수: 아니오
"""


@pytest.fixture(scope="module")
def blind():
    spec = importlib.util.spec_from_file_location("blind_labels", REPO_ROOT / "scripts" / "blind_labels.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["blind_labels"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("blind_labels", None)


def write(folder, name, minutes, labels):
    (folder / f"{name}.txt").write_text(minutes, encoding="utf-8")
    if labels is not None:
        (folder / f"{name}.yaml").write_text(labels, encoding="utf-8")


def test_labels_become_gold_rows(blind, tmp_path):
    write(tmp_path, "team_a", MINUTES, LABELS)
    assert blind.main(["--dir", str(tmp_path)]) == 0
    output = tmp_path / "blind_gold.jsonl"
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["sample"] == "team_a.txt"
    assert row["rules_frozen_at"] == blind.RULES_FROZEN_AT
    assert row["items"] == [
        {"id": "b1-1", "keywords": ["교육 자료"], "owner": "김민준", "due": "2026-10-09", "optional": False,
         "note": "교육 자료 정리"},
        {"id": "b1-2", "keywords": ["장비"], "owner": None, "due": None, "optional": True, "note": "장비 점검"},
    ]
    gold = load_gold(output)[0]
    assert gold.meeting_date.isoformat() == "2026-10-08"
    assert [item.optional for item in gold.items] == [False, True]


def test_meeting_date_and_time_due(blind, tmp_path):
    labels = LABELS.replace("회의날짜: 2026-10-08\n", "").replace("기한: 2026-10-09", "기한: 2026-10-09 15:00")
    write(tmp_path, "team_a", MINUTES, labels)
    rows, problems = blind.convert(tmp_path)
    assert problems == []
    assert rows[0]["meeting_date"] == "2026-10-08"  # from the minutes header
    assert rows[0]["items"][0]["due"] == "2026-10-09 15:00"


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        (LABELS.replace("낱말: [교육 자료]", "낱말: [연수 계획]"), "낱말 '연수 계획'이 회의록에 없습니다"),
        (LABELS.replace("담당자: 김민준", "담당자: 박지훈"), "담당자 '박지훈'이 회의록에 없습니다"),
        (LABELS.replace("기한: 2026-10-09", "기한: 금요일"), "기한 '금요일'은"),
        (LABELS.replace("필수: 예", "필수: 꼭"), "필수는 '예' 또는 '아니오'"),
        (LABELS.replace("낱말: [교육 자료]", "낱말: []"), "낱말은 1~3개"),
        ("회의날짜: 2026-10-08\n항목: []\n", "할 일을 하나 이상"),
        ("항목: [\n", "YAML 형식 오류"),
    ],
)
def test_problems_are_reported_in_plain_korean(blind, tmp_path, capsys, labels, message):
    write(tmp_path, "team_a", MINUTES, labels)
    assert blind.main(["--dir", str(tmp_path)]) == 1
    assert message in capsys.readouterr().out
    assert not (tmp_path / "blind_gold.jsonl").exists()


def test_unpaired_files_and_template(blind, tmp_path, capsys):
    assert blind.main(["--dir", str(tmp_path), "--init"]) == 0
    template = tmp_path / blind.TEMPLATE
    assert "회의날짜" in template.read_text(encoding="utf-8")
    capsys.readouterr()
    write(tmp_path, "no_labels", MINUTES, None)
    (tmp_path / "no_minutes.yaml").write_text(LABELS, encoding="utf-8")
    assert blind.main(["--dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "no_labels.txt: 라벨 파일(no_labels.yaml)이 없습니다." in out
    assert "no_minutes.yaml: 같은 이름의 회의록(no_minutes.txt)이 없습니다." in out
    assert "_양식" not in out  # the template is never read as labels


def test_template_example_is_valid(blind, tmp_path):
    """The template's example item passes the checks against matching minutes."""
    minutes = "회의명: 가상 회의\n일시: 2026.10.08(목)\n- 김민준 주무관: 오리엔테이션 자료를 보완하겠습니다.\n"
    write(tmp_path, "team_a", minutes, blind.TEMPLATE_TEXT)
    rows, problems = blind.convert(tmp_path)
    assert problems == []
    assert rows[0]["items"][0]["keywords"] == ["오리엔테이션", "자료"]
