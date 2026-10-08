"""Headless runs of the Streamlit pages (AppTest), with DRY_RUN and a SQLite store in tmp."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from onboarding_agent.config import REPO_ROOT, get_settings
from onboarding_agent.meeting import extract as extract_module
from onboarding_agent.meeting.models import (
    ActionItem,
    ExtractionResult,
    Meeting,
    ToolCallSummary,
    source_hash,
)
from onboarding_agent.store.sqlite_store import SqliteStore

APP = REPO_ROOT / "app"
TIMEOUT = 30
KST = ZoneInfo("Asia/Seoul")


def fake_extract(text, *, title, meeting_date, source_filename=None, progress=None, **_):
    """Stands in for the LLM: two confirmed items, reported like a function-calling run."""
    if progress:
        progress("llm", "fake-model")
        progress("validate", "")
    meeting = Meeting(
        meeting_id="m-test",
        title=title,
        meeting_date=meeting_date,
        source_filename=source_filename,
        source_hash=source_hash(text, meeting_date),
        summary=["요약"],
        created_at=datetime(2026, 10, 8, 10, tzinfo=KST),
    )
    items = [
        ActionItem(
            item_id=f"item-{n}",
            meeting_id="m-test",
            task=f"할 일 {n} 하기",
            owner_name="김민준",
            owner_slack_id="U00000001",
            owner_status="confirmed",
            due_date=date(2026, 10, 14),
            due_status="confirmed",
            evidence_quote="근거",
        )
        for n in range(2)
    ]
    return ExtractionResult(
        meeting=meeting,
        action_items=items,
        extraction_path="function_calling",
        model_used="fake-model",
        fallback_used=False,
        tool_calls=ToolCallSummary(
            turns=1,
            calls={"record_meeting_overview": 1, "propose_action_item": 2, "finish_extraction": 1},
            finished=True,
        ),
    )


@pytest.fixture
def patched_extract(monkeypatch):
    monkeypatch.setattr(extract_module, "extract_meeting", fake_extract)


def keys(app, kind="button"):
    return {w.key for w in getattr(app, kind)}


def store() -> SqliteStore:
    return SqliteStore(get_settings().sqlite_path)


def load_sample_and_extract(app: AppTest) -> AppTest:
    app.selectbox(key="meeting.sample").set_value("01_structured_minutes.txt").run()
    assert app.text_input(key="meeting.title_input").value == "10월 2주차 신규 임용자 온보딩 점검 회의"
    app.button(key="meeting.extract").click().run()
    assert not app.exception
    return app


def test_main_navigates_to_meeting_and_status_pages():
    app = AppTest.from_file(str(APP / "main.py"), default_timeout=TIMEOUT).run()
    assert not app.exception
    assert app.title[0].value == "규정 Q&A"
    app.switch_page("views/meeting.py").run()
    assert app.title[0].value == "회의록 → 액션"
    app.switch_page("views/status.py").run()
    assert app.title[0].value == "액션 현황"


def test_full_flow_without_duplicates(patched_extract):
    app = AppTest.from_file(str(APP / "views" / "meeting.py"), default_timeout=TIMEOUT).run()
    load_sample_and_extract(app)

    # Tool panel above the results, then approval.
    assert [m.label for m in app.metric][:3] == ["추출 경로", "사용 모델", "턴 수"]
    assert app.metric[0].value == "Function calling"
    assert "meeting.approve" in keys(app)
    app.button(key="meeting.approve").click().run()
    assert not app.exception
    assert "승인 완료" in app.success[0].value
    assert "meeting.approve" not in keys(app)  # nothing left to click twice
    assert app.query_params["meeting"] == "m-test"

    items = store().list_items(meeting_id="m-test")
    assert len(store().list_meetings()) == 1 and len(items) == 2
    first_ids = {i.item_id: (i.calendar_event_id, i.slack_ts) for i in items}
    assert all(cal.startswith("dryrun:") and ts.startswith("dryrun:") for cal, ts in first_ids.values())

    # A refresh is a new session that only has the URL.
    refreshed = AppTest.from_file(str(APP / "views" / "meeting.py"), default_timeout=TIMEOUT)
    refreshed.query_params["meeting"] = "m-test"
    refreshed.run()
    assert not refreshed.exception
    assert "승인 완료" in refreshed.success[0].value
    assert "meeting.approve" not in keys(refreshed)
    table = refreshed.dataframe[0].value
    assert list(table["Calendar"]) == ["DRY_RUN 기록"] * 2
    after = {i.item_id: (i.calendar_event_id, i.slack_ts) for i in store().list_items(meeting_id="m-test")}
    assert after == first_ids


def test_leaving_the_page_and_coming_back_keeps_the_result(patched_extract):
    app = AppTest.from_file(str(APP / "main.py"), default_timeout=TIMEOUT).run()
    app.switch_page("views/meeting.py").run()
    load_sample_and_extract(app)
    app.button(key="meeting.approve").click().run()
    app.switch_page("views/status.py").run()
    assert app.title[0].value == "액션 현황"
    app.switch_page("views/meeting.py").run()
    assert not app.exception
    assert "승인 완료" in app.success[0].value
    assert "meeting.approve" not in keys(app)
    assert len(store().list_items(meeting_id="m-test")) == 2


def test_reprocessing_the_same_minutes_warns(patched_extract, monkeypatch):
    app = AppTest.from_file(str(APP / "views" / "meeting.py"), default_timeout=TIMEOUT).run()
    load_sample_and_extract(app)
    app.button(key="meeting.approve").click().run()
    app.button(key="meeting.new").click().run()

    # Same file again: the fake gives a new meeting id but the same source hash.
    def again(*args, **kwargs):
        first = fake_extract(*args, **kwargs)
        return first.model_copy(update={"meeting": first.meeting.model_copy(update={"meeting_id": "m-again"})})

    monkeypatch.setattr(extract_module, "extract_meeting", again)
    load_sample_and_extract(app)
    assert any("이미 승인된 적이 있습니다" in w.value for w in app.warning)
    assert "meeting.open_previous" in keys(app)


def test_forced_fallback_results_cannot_be_approved_until_confirmed():
    app = AppTest.from_file(str(APP / "views" / "meeting.py"), default_timeout=TIMEOUT).run()
    app.selectbox(key="meeting.sample").set_value("03_edge_cases.txt").run()
    app.toggle(key="meeting.force_input").set_value(True).run()
    app.button(key="meeting.extract").click().run()
    assert not app.exception
    assert app.metric[0].value == "규칙 기반"
    assert app.button(key="meeting.approve").disabled
    app.checkbox(key="meeting.exclude_input").check().run()
    assert app.button(key="meeting.approve").disabled  # every rule-based row is unconfirmed


def test_status_page_lists_approved_items():
    status_store = store()
    status_store.save_meeting(
        Meeting(meeting_id="m1", title="주간 회의", meeting_date=date(2026, 10, 8),
                created_at=datetime(2026, 10, 8, 10, tzinfo=KST))
    )
    status_store.upsert_items(
        [
            ActionItem(item_id="a", meeting_id="m1", task="진행 중 할 일", evidence_quote="q", status="approved"),
            ActionItem(item_id="b", meeting_id="m1", task="초안 할 일", evidence_quote="q"),
        ]
    )
    app = AppTest.from_file(str(APP / "views" / "status.py"), default_timeout=TIMEOUT).run()
    assert not app.exception
    assert list(app.dataframe[0].value["할 일"]) == ["진행 중 할 일"]


def test_status_page_empty_store_shows_info():
    app = AppTest.from_file(str(APP / "views" / "status.py"), default_timeout=TIMEOUT).run()
    assert not app.exception
    assert app.info[0].value == "표시할 액션 아이템이 없습니다."


def test_failed_execution_offers_retry_and_retry_only_redoes_failures(patched_extract, monkeypatch):
    from onboarding_agent import config
    from onboarding_agent.actions import executor

    monkeypatch.setenv("ACTIONS_DRY_RUN", "false")
    config.reset_settings_cache()
    app = AppTest.from_file(str(APP / "views" / "meeting.py"), default_timeout=TIMEOUT).run()
    load_sample_and_extract(app)
    app.button(key="meeting.approve").click().run()
    table = app.dataframe[0].value
    assert list(table["Calendar"]) == ["실패"] * 2 and list(table["Slack"]) == ["실패"] * 2
    assert "설정이 필요합니다: GCAL_CALENDAR_ID" in table["오류"][0]
    assert app.button(key="meeting.retry").label == "실패 항목만 재시도"

    # Credentials arrive; the retry creates each event and the message exactly once.
    created, posted = [], []

    class Calendar:
        def insert_event(self, body):
            created.append(body["id"])
            return body["id"], "created"

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    config.reset_settings_cache()
    monkeypatch.setattr(executor.CalendarClient, "from_settings", classmethod(lambda cls, s: Calendar()))
    monkeypatch.setattr(executor, "post_message", lambda *a, **k: posted.append(a) or "1700000000.000100")
    app.button(key="meeting.retry").click().run()
    table = app.dataframe[0].value
    assert list(table["Calendar"]) == ["생성"] * 2 and list(table["Slack"]) == ["발송"] * 2
    assert len(created) == 2 and len(posted) == 1
    assert "meeting.retry" not in keys(app)
