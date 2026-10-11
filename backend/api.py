"""웹앱 백엔드 (FastAPI). 실행: uvicorn backend.api:app --reload --port 8000"""
import hmac
import os
import sys
import time
from datetime import date, datetime, time as dtime
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import config
from backend.agent.agent import OnboardingAgent
from backend.rag import index as rag
from backend.state import store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # 팀원 회의록 엔진(onboarding_agent)
os.environ.setdefault("SQLITE_PATH", "data/meetings.db")  # 온보딩 진행률 DB(state.db)와 파일을 분리

DEFAULT_EMP = "2026-0101"
ACCESS_CODE = os.getenv("ACCESS_CODE", "")          # 비어 있으면 누구나 접근 (로컬 개발용)
RATE_LIMIT, RATE_WINDOW = 40, 60                    # IP당 분당 40회 (LLM 비용 보호)
MAX_MESSAGE_CHARS = 1000
_hits: dict[str, deque] = defaultdict(deque)
app = FastAPI(title="온보딩 AI Agent")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])
store.seed()
_sessions: dict[str, OnboardingAgent] = {}


def guard(request: Request):
    """접근 코드 확인 + IP별 호출 횟수 제한. 프록시(Render 등) 뒤에서는 X-Forwarded-For의 첫 IP를 쓴다."""
    if ACCESS_CODE and not hmac.compare_digest(request.headers.get("x-access-code", ""), ACCESS_CODE):
        raise HTTPException(401, "접근 코드가 필요해요.")
    ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    q, now = _hits[ip], time.time()
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "요청이 너무 많아요. 잠시 후 다시 시도해 주세요.")
    q.append(now)


api = APIRouter(prefix="/api", dependencies=[Depends(guard)])


def _agent(sid: str) -> OnboardingAgent:
    if sid not in _sessions:
        _sessions[sid] = OnboardingAgent(DEFAULT_EMP, profile="web")
    return _sessions[sid]


class TodoIn(BaseModel):
    title: str
    due_date: str | None = None


class DoneIn(BaseModel):
    done: bool = True


class ChatIn(BaseModel):
    session_id: str
    message: str


@app.get("/api/health")
def health():
    idx = rag.load_index()
    return {"ok": True, "llm": bool(config.OPENAI_API_KEY), "needs_code": bool(ACCESS_CODE),
            "rag_chunks": len(idx[0]) if idx else 0}


@api.post("/chat")
def chat(body: ChatIn):
    if not body.message.strip():
        raise HTTPException(400, "메시지가 비어 있습니다.")
    if len(body.message) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"메시지는 {MAX_MESSAGE_CHARS}자 이하로 입력해 주세요.")
    a = _agent(body.session_id)
    n = len(a.ctx["escalations"])
    reply = a.chat(body.message.strip())
    return {"reply": reply, "mode": a.mode, "escalated": len(a.ctx["escalations"]) > n,
            "escalations": a.ctx["escalations"]}


@api.post("/chat/reset")
def reset(body: ChatIn):
    _sessions.pop(body.session_id, None)
    return {"ok": True}


@api.get("/me")
def me():
    p = store.get_employee(DEFAULT_EMP)
    done, total = store.progress(DEFAULT_EMP)
    tasks = [{**t, "custom": t["task_id"].startswith(store.CUSTOM_PREFIX)} for t in store.get_tasks(DEFAULT_EMP)]
    return {"name": p["name"], "org": p["org"], "dept": p["dept"], "done": done, "total": total, "tasks": tasks}


@api.post("/todos")
def add_todo(body: TodoIn):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "할 일을 입력해 주세요.")
    if len(title) > 120:
        raise HTTPException(400, "할 일은 120자 이하로 입력해 주세요.")
    due = None
    if body.due_date:
        try:
            due = date.fromisoformat(body.due_date).isoformat()
        except ValueError:
            raise HTTPException(400, "날짜 형식이 올바르지 않아요.")
    return store.add_task(DEFAULT_EMP, title, due)


@api.post("/todos/{task_id}/done")
def todo_done(task_id: str, body: DoneIn):
    if not any(t["task_id"] == task_id for t in store.get_tasks(DEFAULT_EMP)):
        raise HTTPException(404, "할 일을 찾을 수 없어요.")
    store.set_task_done(DEFAULT_EMP, task_id, body.done)
    done, total = store.progress(DEFAULT_EMP)
    return {"done": done, "total": total}


@api.delete("/todos/{task_id}")
def todo_delete(task_id: str):
    if not store.delete_task(DEFAULT_EMP, task_id):
        raise HTTPException(404, "직접 추가한 할 일만 지울 수 있어요.")
    return {"ok": True}


# --- 회의록 → 액션 (팀원 엔진) ---
from backend import meeting_api  # noqa: E402

meeting_api.register(api)


app.include_router(api)

_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.exists():  # 빌드된 프론트를 같은 서버에서 제공 (배포용)
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = _DIST / path
        return FileResponse(f if path and f.is_file() else _DIST / "index.html")
