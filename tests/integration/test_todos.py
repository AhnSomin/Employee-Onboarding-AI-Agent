"""할 일 체크리스트: 직접 체크, 내 할 일 추가·삭제, 기본 항목은 삭제 불가."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from backend import api, config
    from backend.state import store
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(api, "ACCESS_CODE", "")
    api._hits.clear()
    store.seed(reset=True)
    return TestClient(api.app)


def test_toggle_updates_progress_and_back(client):
    me = client.get("/api/me").json()
    first = me["tasks"][0]["task_id"]
    r = client.post(f"/api/todos/{first}/done", json={"done": True}).json()
    assert r["done"] == 1 and r["total"] == me["total"]
    assert client.get("/api/me").json()["done"] == 1
    assert client.post(f"/api/todos/{first}/done", json={"done": False}).json()["done"] == 0


def test_add_custom_todo_with_due_date_and_delete_it(client):
    total = client.get("/api/me").json()["total"]
    added = client.post("/api/todos", json={"title": "  출입증 사진 제출  ", "due_date": "2026-10-20"}).json()
    me = client.get("/api/me").json()
    mine = next(t for t in me["tasks"] if t["task_id"] == added["task_id"])
    assert me["total"] == total + 1 and mine["title"] == "출입증 사진 제출"      # 공백 정리
    assert mine["custom"] is True and mine["due_date"] == "2026-10-20"
    assert client.delete(f"/api/todos/{added['task_id']}").status_code == 200
    assert client.get("/api/me").json()["total"] == total


def test_template_items_cannot_be_deleted_but_can_be_checked(client):
    base = client.get("/api/me").json()["tasks"][0]
    assert base["custom"] is False
    assert client.delete(f"/api/todos/{base['task_id']}").status_code == 404
    assert client.post(f"/api/todos/{base['task_id']}/done", json={"done": True}).status_code == 200


def test_validation(client):
    assert client.post("/api/todos", json={"title": "   "}).status_code == 400
    assert client.post("/api/todos", json={"title": "x" * 121}).status_code == 400
    assert client.post("/api/todos", json={"title": "ok", "due_date": "내일"}).status_code == 400
    assert client.post("/api/todos/nope/done", json={"done": True}).status_code == 404
