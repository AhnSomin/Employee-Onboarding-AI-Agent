"""온보딩 상태 저장소 (SQLite). 배치 스케줄러와 대화 에이전트가 이 모듈 하나를 공유한다."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

from backend import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (emp_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  emp_id TEXT, task_id TEXT, title TEXT, phase TEXT, due_date TEXT,
  done INTEGER DEFAULT 0, last_reminded TEXT,
  PRIMARY KEY (emp_id, task_id));
CREATE TABLE IF NOT EXISTS leave_requests (
  req_id INTEGER PRIMARY KEY AUTOINCREMENT, emp_id TEXT, payload TEXT,
  status TEXT, created_at TEXT, submitted_at TEXT);
"""


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def seed(reset: bool = False):
    """profiles.json + 로드맵 템플릿으로 DB 초기화."""
    profiles = json.loads((config.DATA_DIR / "profiles.json").read_text())
    tpl = json.loads((config.DATA_DIR / "roadmap_templates.json").read_text())
    with conn() as c:
        if reset:
            for t in ("employees", "tasks", "leave_requests"):
                c.execute(f"DELETE FROM {t}")
        for p in profiles:
            old = c.execute("SELECT data FROM employees WHERE emp_id=?", (p["emp_id"],)).fetchone()
            if old:  # 프로필 정보는 최신으로, 사용한 연가 같은 상태값은 유지
                p = {**p, "leave_used": json.loads(old["data"]).get("leave_used", p["leave_used"])}
            c.execute("INSERT OR REPLACE INTO employees VALUES (?,?)", (p["emp_id"], json.dumps(p, ensure_ascii=False)))
            hire = date.fromisoformat(p["hire_date"])
            for t in tpl["common"] + tpl["by_job"].get(p["job"], []):
                due = hire + timedelta(days=t["due_offset_days"])
                c.execute("INSERT OR IGNORE INTO tasks (emp_id,task_id,title,phase,due_date) VALUES (?,?,?,?,?)",
                          (p["emp_id"], t["id"], t["title"], t["phase"], due.isoformat()))


def list_employees() -> list[dict]:
    with conn() as c:
        return [json.loads(r["data"]) for r in c.execute("SELECT data FROM employees")]


def get_employee(emp_id: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT data FROM employees WHERE emp_id=?", (emp_id,)).fetchone()
    if not r:
        raise KeyError(emp_id)
    return json.loads(r["data"])


def leave_remaining(emp_id: str) -> float:
    p = get_employee(emp_id)
    return p["leave_total"] - p["leave_used"]


def adjust_leave_used(emp_id: str, days: float):
    p = get_employee(emp_id)
    p["leave_used"] += days
    with conn() as c:
        c.execute("UPDATE employees SET data=? WHERE emp_id=?",
                  (json.dumps(p, ensure_ascii=False), emp_id))


def get_tasks(emp_id: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE emp_id=? ORDER BY due_date, task_id", (emp_id,))]


def set_task_done(emp_id: str, task_id: str, done: bool = True):
    with conn() as c:
        c.execute("UPDATE tasks SET done=? WHERE emp_id=? AND task_id=?",
                  (int(done), emp_id, task_id))


def progress(emp_id: str) -> tuple[int, int]:
    ts = get_tasks(emp_id)
    return sum(t["done"] for t in ts), len(ts)


def mark_reminded(emp_id: str, task_id: str, day: date):
    with conn() as c:
        c.execute("UPDATE tasks SET last_reminded=? WHERE emp_id=? AND task_id=?",
                  (day.isoformat(), emp_id, task_id))


def save_leave_request(emp_id: str, payload: dict, status: str = "DRAFT") -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO leave_requests (emp_id,payload,status,created_at) VALUES (?,?,?,?)",
            (emp_id, json.dumps(payload, ensure_ascii=False), status, config.today().isoformat()))
        return cur.lastrowid


def get_leave_request(req_id: int) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM leave_requests WHERE req_id=?", (req_id,)).fetchone()
    if not r:
        raise KeyError(req_id)
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    return d


def update_leave_status(req_id: int, status: str):
    with conn() as c:
        c.execute("UPDATE leave_requests SET status=?, submitted_at=? WHERE req_id=?",
                  (status, config.today().isoformat(), req_id))


def list_requests(emp_id: str | None = None, status: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM leave_requests WHERE 1=1", []
    if emp_id:
        q += " AND emp_id=?"; args.append(emp_id)
    if status:
        q += " AND status=?"; args.append(status)
    with conn() as c:
        rows = [dict(r) for r in c.execute(q + " ORDER BY req_id DESC", args)]
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


def update_request_payload(req_id: int, payload: dict):
    with conn() as c:
        c.execute("UPDATE leave_requests SET payload=? WHERE req_id=?",
                  (json.dumps(payload, ensure_ascii=False), req_id))
