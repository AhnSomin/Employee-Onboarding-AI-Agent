import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from onboarding_agent import metrics
from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting import review
from onboarding_agent.meeting.models import (
    ActionItem,
    Decision,
    ExtractionResult,
    Meeting,
)
from onboarding_agent.meeting.roster import load_roster
from onboarding_agent.store.sqlite_store import SqliteStore

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 8, 11, tzinfo=KST)
ROSTER = load_roster(REPO_ROOT / "data" / "roster.yaml")


def result() -> ExtractionResult:
    meeting = Meeting(
        meeting_id="m1",
        title="주간 회의",
        meeting_date=date(2026, 10, 8),
        summary=["요약"],
        decisions=[Decision(decision_id="d1", text="위키에 올린다.", evidence_quote="근거")],
        created_at=NOW,
    )
    confirmed = ActionItem(
        item_id="a",
        meeting_id="m1",
        task="자료 보완하기",
        owner_name="김민준",
        owner_slack_id="U00000001",
        owner_status="confirmed",
        due_date=date(2026, 10, 14),
        due_status="confirmed",
        evidence_quote="근거 a",
    )
    open_item = ActionItem(
        item_id="b",
        meeting_id="m1",
        task="계정 요청 전달하기",
        owner_name="박 주무관",
        due_date=date(2026, 10, 10),
        due_status="confirmed",
        evidence_quote="근거 b",
        review_notes=["담당자 후보가 여럿입니다: 박지훈, 박소연"],
    )
    return ExtractionResult(
        meeting=meeting,
        action_items=[confirmed, open_item],
        extraction_path="function_calling",
        model_used="m",
        fallback_used=False,
    )


def test_rows_show_status_and_weekend_flag():
    rows = review.rows_from_result(result())
    assert rows[0]["status"] == "확정"
    assert rows[1]["status"] == "미확정(담당) · ⚠ 주말"  # 10/10 is a Saturday
    assert rows[1]["owner"] == "박 주무관"
    assert review.owner_options(ROSTER, rows)[:2] == ["미확정", "김민준"]
    assert "박 주무관" in review.owner_options(ROSTER, rows)


def test_picking_an_owner_or_date_confirms_it():
    rows = review.rows_from_result(result())
    rows = review.apply_edits(rows, {1: {"owner": "박지훈"}}, [], [])
    assert rows[1]["owner_ok"] and rows[1]["status"] == "확정 · ⚠ 주말"
    rows = review.apply_edits(rows, {1: {"due_date": "2026-10-12", "due_time": "10:30:00"}}, [], [])
    assert rows[1]["due_date"] == date(2026, 10, 12) and rows[1]["due_time"] == time(10, 30)
    assert rows[1]["status"] == "확정"
    rows = review.apply_edits(rows, {1: {"owner": "미확정"}}, [], [])
    assert not rows[1]["owner_ok"]


def test_explicit_checkbox_wins_and_cannot_confirm_empty_values():
    rows = review.rows_from_result(result())
    rows = review.apply_edits(rows, {0: {"due_ok": False}}, [], [])
    assert rows[0]["status"] == "미확정(기한)"
    rows = review.apply_edits(rows, {0: {"due_date": None}}, [], [])
    rows = review.apply_edits(rows, {0: {"due_ok": True}}, [], [])
    assert rows[0]["due_ok"] is False


def test_rows_can_be_added_and_deleted():
    rows = review.rows_from_result(result())
    rows = review.apply_edits(rows, {}, [{"task": "새 일 하기", "owner": "정하은", "due_date": "2026-10-20"}], [0])
    assert [r["task"] for r in rows] == ["계정 요청 전달하기", "새 일 하기"]
    added = rows[1]
    assert added["owner_ok"] and added["due_ok"] and added["notes"] == review.USER_ADDED_NOTE


def test_unconfirmed_rows_block_approval_unless_excluded():
    rows = review.rows_from_result(result())
    blocked = review.plan_approval(rows, exclude_unconfirmed=False)
    assert blocked.problems == ["2행 '계정 요청 전달하기': 담당자 미확정"]
    allowed = review.plan_approval(rows, exclude_unconfirmed=True)
    assert [r["item_id"] for r in allowed.approve] == ["a"]
    assert [r["item_id"] for r in allowed.exclude] == ["b"]
    nothing = review.plan_approval([{**rows[0], "include": False}], exclude_unconfirmed=False)
    assert "승인할 항목이 없습니다" in nothing.problems[0]


def test_modified_cells_count_edits_additions_and_deletions():
    original = review.rows_from_result(result())
    edited = review.apply_edits(original, {1: {"owner": "박지훈"}}, [{"task": "새 일"}], [0])
    assert review.count_modified_cells(original, edited) == (1 + 4 + 4, 8)


def test_approval_saves_approved_and_draft_items_with_slack_ids(tmp_path):
    store = SqliteStore(tmp_path / "state.db")
    rows = review.apply_edits(review.rows_from_result(result()), {1: {"owner": "박지훈"}}, [], [])
    rows = review.apply_edits(rows, {0: {"include": False}}, [], [])
    approval = review.approve_meeting(
        store, result(), rows, summary=["새 요약", ""], decisions=["위키에 올린다.", "새 결정"],
        exclude_unconfirmed=False, roster=ROSTER, now=NOW,
    )
    assert approval.meeting is not None
    saved = {i.item_id: i for i in store.list_items(meeting_id="m1")}
    assert saved["a"].status == "draft" and saved["a"].approved_at is None
    assert saved["b"].status == "approved" and saved["b"].approved_at == NOW
    assert (saved["b"].owner_name, saved["b"].owner_slack_id) == ("박지훈", None)  # no Slack ID in the roster
    meeting = store.get_meeting("m1")
    assert meeting.summary == ["새 요약"]
    assert [d.decision_id for d in meeting.decisions][0] == "d1"  # unchanged line keeps its evidence
    record = json.loads(metrics.LOG_PATH.read_text(encoding="utf-8").splitlines()[-1])
    assert (record["event"], record["approved"], record["not_included"]) == ("meeting_approved", 1, 1)
    assert record["modified_cells"] == 1


def test_failed_plan_saves_nothing(tmp_path):
    store = SqliteStore(tmp_path / "state.db")
    approval = review.approve_meeting(
        store, result(), review.rows_from_result(result()), summary=[], decisions=[],
        exclude_unconfirmed=False, roster=ROSTER, now=NOW,
    )
    assert approval.meeting is None and approval.plan.problems
    assert store.list_meetings() == []
