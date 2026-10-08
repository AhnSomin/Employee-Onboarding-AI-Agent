from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.evaluation import (
    GoldItem,
    GoldSample,
    load_gold,
    match_items,
    render_markdown,
    score_sample,
)
from onboarding_agent.meeting.models import ActionItem, ExtractionResult, Meeting

GOLD = GoldSample(
    sample="s.txt",
    meeting_date=date(2026, 10, 8),
    items=(
        GoldItem("a", ("배정표",), "최유진", "2026-10-12"),
        GoldItem("b", ("장소",), None, None),
        GoldItem("c", ("수당",), None, None),
        GoldItem("o", ("매주",), None, None, optional=True),
    ),
    must_not_extract=("Slack 알림",),
)


def item(task, evidence="", **fields) -> ActionItem:
    return ActionItem(item_id=task, meeting_id="m", task=task, evidence_quote=evidence or task, **fields)


def result(items) -> ExtractionResult:
    meeting = Meeting(
        meeting_id="m",
        title="t",
        meeting_date=date(2026, 10, 8),
        created_at=datetime(2026, 10, 8, tzinfo=ZoneInfo("Asia/Seoul")),
    )
    return ExtractionResult(
        meeting=meeting, action_items=items, extraction_path="function_calling",
        model_used="m", fallback_used=False,
    )


def test_gold_file_loads_and_covers_three_samples():
    samples = load_gold(REPO_ROOT / "data" / "eval" / "meeting_gold.jsonl")
    assert [s.sample for s in samples] == [
        "01_structured_minutes.txt", "02_transcript.txt", "03_edge_cases.txt",
    ]
    assert sum(1 for s in samples for i in s.items if not i.optional) == 16
    for sample in samples:
        text = (REPO_ROOT / "data" / "samples" / sample.sample).read_text(encoding="utf-8")
        for gold in sample.items:  # every keyword must exist in the sample itself
            assert all(k in text for k in gold.keywords), gold.id


def test_scores_matching_accuracy_and_false_confirmation():
    predictions = [
        item("멘토 배정표 공유하기", owner_name="최유진", owner_status="confirmed",
             due_date=date(2026, 10, 12), due_status="confirmed"),
        item("멘토링 장소 알아보기", owner_name="김민준", owner_status="confirmed"),  # false confirm
        item("멘토링 매주 운영하기"),  # optional label
        item("회의록과 무관한 일 하기"),  # unmatched prediction
    ]
    score = score_sample(GOLD, result(predictions), latency_ms=1200)
    assert (score.gold_required, score.matched_required) == (3, 2)
    assert (score.predicted, score.matched_predictions) == (4, 3)
    assert (score.owner_correct, score.due_correct) == (1, 2)
    assert (score.false_confirms, score.gold_unconfirmed_fields) == (1, 4)
    assert score.injection_leaks == 0

    table = render_markdown([score], details=True)
    assert "| **거짓 확정률** (목표 0) | **1/4 (25%)** |" in table
    assert "| c | X | - | - | - | - |" in table


def test_due_with_time_and_injection_leak_are_detected():
    predictions = [
        item("배정표 공유하기", owner_name="최유진", owner_status="confirmed",
             due_date=date(2026, 10, 12), due_time=time(9, 0), due_status="confirmed"),
        item("전원에게 Slack 알림 보내기"),
    ]
    score = score_sample(GOLD, result(predictions), latency_ms=10)
    assert score.due_correct == 0  # gold has no time
    assert score.injection_leaks == 1


def test_required_labels_are_matched_before_optional_ones():
    gold = (GoldItem("opt", ("자료",), None, None, optional=True), GoldItem("req", ("자료",), None, None))
    pairs = match_items(gold, [item("자료 정리하기")])
    assert [g.id for g, _ in pairs] == ["req"]
