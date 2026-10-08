"""Headless render checks for the Streamlit entrypoint and the meeting pages."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from onboarding_agent.config import REPO_ROOT, get_settings
from onboarding_agent.meeting.models import ActionItem, Meeting, new_id
from onboarding_agent.store.sqlite_store import SqliteStore

APP = REPO_ROOT / "app"
TIMEOUT = 30


def test_main_navigates_to_meeting_and_status_pages():
    app = AppTest.from_file(str(APP / "main.py"), default_timeout=TIMEOUT).run()
    assert not app.exception
    assert app.title[0].value == "규정 Q&A"

    app.switch_page("views/meeting.py").run()
    assert not app.exception
    assert app.title[0].value == "회의록 → 액션"

    app.switch_page("views/status.py").run()
    assert not app.exception
    assert app.title[0].value == "액션 현황"


def test_meeting_page_shows_dry_run_and_disabled_extract():
    app = AppTest.from_file(str(APP / "views" / "meeting.py"), default_timeout=TIMEOUT).run()
    assert not app.exception
    assert app.button(key="meeting.extract").disabled


def test_status_page_lists_approved_items():
    store = SqliteStore(get_settings().sqlite_path)
    store.save_meeting(
        Meeting(
            meeting_id="m1",
            title="주간 회의",
            meeting_date=date(2026, 10, 8),
            created_at=datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        )
    )
    store.upsert_items(
        [
            ActionItem(item_id=new_id(), meeting_id="m1", task="진행 중 할 일", evidence_quote="q", status="approved"),
            ActionItem(item_id=new_id(), meeting_id="m1", task="초안 할 일", evidence_quote="q"),
        ]
    )

    app = AppTest.from_file(str(APP / "views" / "status.py"), default_timeout=TIMEOUT).run()
    assert not app.exception
    table = app.dataframe[0].value
    assert list(table["할 일"]) == ["진행 중 할 일"]


def test_status_page_empty_store_shows_info():
    app = AppTest.from_file(str(APP / "views" / "status.py"), default_timeout=TIMEOUT).run()
    assert not app.exception
    assert app.info[0].value == "표시할 액션 아이템이 없습니다."
