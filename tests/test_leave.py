import os
from datetime import date

import pytest

os.environ["DEMO_TODAY"] = "2026-10-06"  # 화요일


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    from backend import config
    from backend.state import store
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")  # 테스트는 규칙 기반 폴백 고정
    store.seed(reset=True)


def test_parse_dates():
    from backend.actions.leave import parse_korean_date as p
    assert p("다음주 금요일에 연가") == date(2026, 10, 16)
    assert p("내일") == date(2026, 10, 7)
    assert p("10월 20일") == date(2026, 10, 20)


def test_leave_guide_flow_no_submission():
    from backend.agent.agent import OnboardingAgent
    from backend.state import store
    a = OnboardingAgent("2026-0101")
    r1 = a.chat("다음주 금요일에 연가 쓰고 싶은데 어떻게 해야 해요?")
    assert "종일" in r1 and "반차" in r1          # 날짜는 알았으니 유형만 되묻기
    r2 = a.chat("종일이요")
    assert "2026-10-16" in r2 and "자동 입력" in r2 and "박정훈" in r2   # 프로필 자동 채움
    assert "MOCK" not in r2                                           # Agent는 상신하지 않음
    form = store.list_requests("2026-0101", "FORM_FILLED")[0]
    assert store.leave_remaining("2026-0101") == 3                    # 상신 전에는 차감 없음
    from backend.actions import leave
    res = leave.submit_form(form["req_id"])                           # 사용자가 시스템에서 '상신'
    assert res["문서번호"].startswith("MOCK-") and store.leave_remaining("2026-0101") == 2
    assert "error" in leave.submit_form(form["req_id"])               # 중복 상신 불가


def test_weekend_rejected_and_escalation():
    from backend.agent.agent import OnboardingAgent
    a = OnboardingAgent("2026-0101")
    assert "주말" in a.chat("이번주 토요일 종일 연가")
    assert "담당자" in a.chat("구내식당 메뉴가 뭐예요")
    assert a.ctx["escalations"]


def test_supply_form_then_user_submits_updates_roadmap():
    from backend.actions import supply
    from backend.agent.agent import OnboardingAgent
    from backend.state import store
    a = OnboardingAgent("2026-0101")
    done0, _ = store.progress("2026-0101")
    assert "몇" in a.chat("키보드 신청하고 싶어요")
    r = a.chat("2개요")
    assert "김하늘" in r and "박정훈" in r and "2개" in r and "MOCK" not in r
    assert store.progress("2026-0101")[0] == done0            # 상신 전에는 로드맵 변화 없음
    form = store.list_requests("2026-0101", "FORM_FILLED")[0]
    res = supply.submit_form(form["req_id"])                  # 사용자가 시스템에서 '상신'
    assert res["문서번호"].startswith("MOCK-S")
    assert store.progress("2026-0101")[0] == done0 + 1


def test_roadmap_progress_text():
    from backend.agent.agent import OnboardingAgent
    a = OnboardingAgent("2026-0101")
    assert "진행률" in a.chat("내 진행률 알려줘")


def test_suggest_and_holiday_rejected():
    from backend.agent.agent import OnboardingAgent
    from backend.actions import calendar_kr
    from datetime import date
    sugs = calendar_kr.suggest_dates(date(2026, 10, 6))
    assert sugs[0]["date"] == "2026-10-08" and sugs[0]["연휴일수"] == 4   # 목 연가 + 한글날(금) + 주말
    a = OnboardingAgent("2026-0101")
    assert "공휴일" in a.chat("10월 9일 종일 연가 쓸래")


def test_ics_and_stale_reminder(monkeypatch):
    from backend.actions import ics, leave
    from backend.agent.agent import OnboardingAgent
    from backend.state import store
    from scheduler import remind
    from datetime import date
    a = OnboardingAgent("2026-0101")
    a.chat("다음주 금요일 종일 연가")
    rid = store.list_requests("2026-0101", "FORM_FILLED")[0]["req_id"]
    leave.submit_form(rid)
    r = store.list_requests("2026-0101")[0]
    assert "DTSTART;VALUE=DATE:20261016" in ics.leave_ics(r["payload"], rid)
    assert not remind.stale_approvals(date(2026, 10, 7))
    stale = remind.stale_approvals(date(2026, 10, 9))
    assert stale and "박정훈" in remind.approval_message(stale[0])


def test_user_can_edit_form_before_submit():
    from backend.actions import leave, supply
    from backend.agent.agent import OnboardingAgent
    from backend.state import store
    a = OnboardingAgent("2026-0101")
    a.chat("다음주 금요일 종일 연가")
    rid = store.list_requests("2026-0101", "FORM_FILLED")[0]["req_id"]
    assert "error" in leave.submit_form(rid, {"일자": "2026-10-17", "시간구분": "종일", "사유": "x"})  # 토요일 거절
    assert store.list_requests("2026-0101", "FORM_FILLED")                                       # 거절 시 폼 유지
    leave.submit_form(rid, {"일자": "2026-10-19", "시간구분": "오전반차", "사유": "병원"})
    r = store.get_leave_request(rid)
    assert r["payload"]["일자"] == "2026-10-19" and r["payload"]["일수"] == 0.5 and r["payload"]["사유"] == "병원"
    assert store.leave_remaining("2026-0101") == 2.5
    a.chat("볼펜 3개")
    sid = store.list_requests("2026-0101", "FORM_FILLED")[0]["req_id"]
    supply.submit_form(sid, {"품목": "노트", "수량": 5, "사유": "회의용"})
    assert store.get_leave_request(sid)["payload"]["품목"] == "노트" and store.get_leave_request(sid)["payload"]["수량"] == "5권"


def test_reseed_does_not_reset_state():
    from backend.actions import leave
    from backend.agent.agent import OnboardingAgent
    from backend.state import store
    OnboardingAgent("2026-0101").chat("다음주 금요일 종일 연가")
    leave.submit_form(store.list_requests("2026-0101", "FORM_FILLED")[0]["req_id"])
    store.seed()  # Streamlit이 매 실행마다 호출
    assert store.leave_remaining("2026-0101") == 2
