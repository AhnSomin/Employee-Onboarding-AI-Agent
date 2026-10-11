"""캘린더 파일(.ics)과 'Slack은 발송, 캘린더는 파일' 흐름. Slack은 가짜 함수로 바꿔 실제 채널에는 가지 않는다."""
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from onboarding_agent.meeting.ics import build_ics
from onboarding_agent.meeting.models import ActionItem, Meeting

MEETING = Meeting(meeting_id="m1", title="주간 회의, 점검; 확인", meeting_date=date(2026, 10, 8),
                  created_at=datetime(2026, 10, 8, 10, tzinfo=ZoneInfo("Asia/Seoul")))


def item(**kw):
    d = dict(item_id="i1", meeting_id="m1", task="자료 보완하기", owner_name="김민준", owner_status="confirmed",
             due_date=date(2026, 10, 12), due_time=time(15, 0), due_status="confirmed",
             evidence_quote="자료는 김민준이 맡기로 함.", status="approved")
    d.update(kw)
    return ActionItem(**d)


def test_timed_event_is_written_in_utc_and_all_day_as_dates():
    ics, n = build_ics(MEETING, [item(), item(item_id="i2", due_time=None, due_date=date(2026, 10, 14))])
    assert n == 2 and ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20261012T060000Z" in ics and "DTEND:20261012T070000Z" in ics     # 15:00 KST = 06:00Z, 1시간
    assert "DTSTART;VALUE=DATE:20261014" in ics and "DTEND;VALUE=DATE:20261015" in ics  # 종일, 끝 날짜는 다음 날
    assert ics.count("BEGIN:VEVENT") == 2 and "SUMMARY:자료 보완하기 (김민준)" in ics


def test_special_characters_are_escaped_and_long_lines_folded():
    ics, _ = build_ics(MEETING, [item(evidence_quote="가" * 120)])
    assert "주간 회의\\, 점검\\; 확인" in ics                                          # 쉼표·세미콜론 이스케이프
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))        # 75바이트 접기
    unfolded = ics.replace("\r\n ", "")
    assert "가" * 120 in unfolded                                                      # 접어도 내용은 그대로


def test_items_without_a_due_date_become_no_event():
    ics, n = build_ics(MEETING, [item(due_date=None, due_time=None, due_status="unconfirmed")])
    assert n == 0 and "BEGIN:VEVENT" not in ics


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FORCE_FALLBACK", "true")
    monkeypatch.setenv("ACTIONS_DRY_RUN", "false")          # 실제 발송 모드 (Slack은 아래에서 가짜로 교체)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C0TEST")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GCAL_CALENDAR_ID", raising=False)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "m.db"))
    from onboarding_agent import config
    config.reset_settings_cache()
    from onboarding_agent.actions import executor
    sent = []

    def fake_post(channel, text, blocks=None, thread_ts=None, **_):
        sent.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return f"1760000000.{len(sent):06d}"

    monkeypatch.setattr(executor, "post_message", fake_post)
    from backend import api, config as bconfig
    monkeypatch.setattr(bconfig, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(api, "ACCESS_CODE", "")
    api._hits.clear()
    c = TestClient(api.app)
    c.sent = sent
    yield c
    config.reset_settings_cache()


def _approve(c):
    ex = c.post("/api/meeting/extract", data={"sample": "01_structured_minutes.txt"}).json()
    rows = [{k: r[k] for k in ("item_id", "include", "task", "owner", "owner_ok", "due_date", "due_time", "due_ok")} for r in ex["rows"]]
    for r in rows:
        r.update(owner=ex["owner_options"][1], owner_ok=True, due_date="2026-10-20", due_ok=True)
    return c.post("/api/meeting/approve", json={"extraction_id": ex["extraction_id"], "rows": rows,
                                                  "summary": ex["summary"], "decisions": ex["decisions"], "exclude_unconfirmed": False}).json()


def test_slack_is_sent_and_calendar_becomes_a_file_when_google_is_not_set_up(client):
    out = _approve(client)
    rep = out["report"]
    assert rep["dry_run"] is False and rep["calendar_via"] == "file" and not rep["failed"]
    assert {(r["calendar"], r["slack"]) for r in rep["results"]} == {("file", "sent")}
    assert len(client.sent) == 1 and client.sent[0]["channel"] == "C0TEST"           # 요약은 한 건만 보낸다
    assert client.sent[0]["text"].startswith("안녕하세요,")
    assert out["ics_events"] == len(rep["results"]) and "BEGIN:VEVENT" in out["ics"]


def test_rerun_does_not_send_or_duplicate_and_the_file_can_be_downloaded_again(client):
    out = _approve(client)
    again = client.post(f"/api/meeting/{out['meeting_id']}/execute").json()
    assert len(client.sent) == 1 and {r["slack"] for r in again["results"]} == {"skipped_existing"}
    f = client.get(f"/api/meeting/{out['meeting_id']}/calendar").json()
    assert f["events"] == out["ics_events"] and f["ics"].startswith("BEGIN:VCALENDAR")
    assert client.get("/api/meeting/nope/calendar").status_code == 404
