"""회의록 API: LLM 없이(FORCE_FALLBACK) 규칙 기반 경로로 추출 → 승인 → 시뮬레이션 실행 → 현황."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FORCE_FALLBACK", "true")
    monkeypatch.setenv("ACTIONS_DRY_RUN", "true")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("ACCESS_CODE", "")
    from onboarding_agent import config
    config.reset_settings_cache()
    from backend import api, config as bconfig
    monkeypatch.setattr(bconfig, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(api, "ACCESS_CODE", "")
    api._hits.clear()
    yield TestClient(api.app)
    config.reset_settings_cache()


def _rows(extraction):
    keys = ("item_id", "include", "task", "owner", "owner_ok", "due_date", "due_time", "due_ok")
    return [{k: r[k] for k in keys} for r in extraction["rows"]]


def test_extract_approve_execute_list(client):
    assert "01_structured_minutes.txt" in client.get("/api/meeting/samples").json()
    ex = client.post("/api/meeting/extract", data={"sample": "01_structured_minutes.txt"}).json()
    assert ex["path"] == "rule_based" and ex["dry_run"] is True and ex["rows"]
    rows = _rows(ex)
    # 규칙 기반 추출은 담당자·기한을 자동 확정하지 않는다 → 아무도 확정하지 않으면 승인할 항목이 없다
    nothing = client.post("/api/meeting/approve", json={
        "extraction_id": ex["extraction_id"], "rows": rows, "summary": [], "decisions": [], "exclude_unconfirmed": True})
    assert nothing.status_code == 422 and "승인할 항목" in nothing.json()["detail"]
    rows[0].update(owner=ex["owner_options"][1], owner_ok=True, due_date="2026-10-30", due_ok=True)   # 사람이 확정
    done = client.post("/api/meeting/approve", json={
        "extraction_id": ex["extraction_id"], "rows": rows, "summary": ex["summary"],
        "decisions": ex["decisions"], "exclude_unconfirmed": True})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["report"]["dry_run"] is True and body["approved"] == 1 and body["excluded"] == len(rows) - 1
    assert all(r["calendar"] == "dry_run" for r in body["report"]["results"])
    listed = client.get("/api/meeting/list").json()
    assert listed and listed[0]["items"]
    # 다시 실행해도 중복되지 않는다 (멱등)
    again = client.post(f"/api/meeting/{body['meeting_id']}/execute").json()
    assert not again["failed"]


def test_unconfirmed_blocks_approval_unless_excluded(client):
    ex = client.post("/api/meeting/extract", data={"sample": "01_structured_minutes.txt"}).json()
    rows = _rows(ex)
    assert any(not (r["owner_ok"] and r["due_ok"]) for r in rows)
    blocked = client.post("/api/meeting/approve", json={
        "extraction_id": ex["extraction_id"], "rows": rows, "summary": [], "decisions": [], "exclude_unconfirmed": False})
    assert blocked.status_code == 422 and "미확정" in blocked.json()["detail"]


def test_user_edit_confirms_owner_and_due(client):
    ex = client.post("/api/meeting/extract", data={"sample": "01_structured_minutes.txt"}).json()
    rows = _rows(ex)
    for r in rows:
        r["owner"], r["owner_ok"] = (ex["owner_options"][1], True)
        r["due_date"], r["due_ok"] = ("2026-10-30", True)
    ok = client.post("/api/meeting/approve", json={
        "extraction_id": ex["extraction_id"], "rows": rows, "summary": [], "decisions": [], "exclude_unconfirmed": False})
    assert ok.status_code == 200 and ok.json()["approved"] == len(rows)


def test_unknown_extraction_and_bad_sample(client):
    assert client.post("/api/meeting/approve", json={"extraction_id": "nope", "rows": []}).status_code == 404
    assert client.post("/api/meeting/extract", data={"sample": "../../etc/passwd"}).status_code == 404
    assert client.post("/api/meeting/extract").status_code == 400


def test_list_reads_items_once_for_all_meetings(client, monkeypatch):
    for sample in ("01_structured_minutes.txt", "02_transcript.txt"):
        ex = client.post("/api/meeting/extract", data={"sample": sample}).json()
        rows = _rows(ex)
        for r in rows:
            r.update(owner=ex["owner_options"][1], owner_ok=True, due_date="2026-10-30", due_ok=True)
        ok = client.post("/api/meeting/approve", json={
            "extraction_id": ex["extraction_id"], "rows": rows, "summary": [], "decisions": [], "execute": False})
        assert ok.status_code == 200 and ok.json()["approved"] == len(rows)
    from backend import meeting_api
    store = meeting_api.get_store()
    calls = []

    class CountingStore:   # Sheets 저장소는 list_items를 부를 때마다 시트 전체를 읽는다 (분당 60회 한도)
        def __getattr__(self, name):
            return getattr(store, name)

        def list_items(self, **kwargs):
            calls.append(kwargs)
            return store.list_items(**kwargs)

    monkeypatch.setattr(meeting_api, "get_store", lambda settings=None: CountingStore())
    listed = client.get("/api/meeting/list").json()
    expected = {m.meeting_id: len(store.list_items(meeting_id=m.meeting_id)) for m in store.list_meetings()}
    assert len(expected) == 2 and all(expected.values())
    assert {m["meeting_id"]: len(m["items"]) for m in listed} == expected
    assert len(calls) == 1
