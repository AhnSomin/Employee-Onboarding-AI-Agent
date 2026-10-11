"""eval_meeting.py: call estimate, saving raw outputs and re-scoring them without the model."""

import importlib.util
import json
import sys

import pytest

from onboarding_agent.config import REPO_ROOT


@pytest.fixture(scope="module")
def eval_script():
    spec = importlib.util.spec_from_file_location("eval_meeting", REPO_ROOT / "scripts" / "eval_meeting.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_meeting"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("eval_meeting", None)


def test_plan_prints_the_estimate_and_calls_nothing(eval_script, capsys, monkeypatch):
    monkeypatch.setattr(eval_script, "extract_meeting", lambda *a, **k: pytest.fail("no extraction with --plan"))
    assert eval_script.main(["--plan", "--repeat", "2"]) == 0
    assert "예상 Gemini 호출: 보통 6~12회, 최대 30회 (샘플 3 × 반복 2" in capsys.readouterr().err


def test_saved_raw_outputs_rescore_to_the_same_numbers(eval_script, tmp_path, capsys):
    raw = tmp_path / "raw.jsonl"
    assert eval_script.main(["--force-fallback", "--save-raw", str(raw), "--details"]) == 0
    first = capsys.readouterr().out
    rows = [json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines()]
    assert {r["sample"] for r in rows} == {"01_structured_minutes.txt", "02_transcript.txt", "03_edge_cases.txt"}
    assert all(r["path"] == "rule_based" and r["extraction"]["action_items"] for r in rows)

    assert eval_script.main(["--rescore", str(raw), "--details"]) == 0
    again = capsys.readouterr().out
    assert "모델 호출 0회" in again
    table = lambda text: text[text.index("| 지표 |") : text.index("평균 추출 지연")]  # noqa: E731
    assert table(again) == table(first)
