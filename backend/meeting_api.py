"""회의록 → 액션 API. 팀원이 만든 엔진(onboarding_agent)을 웹앱 서버에 연결한다.

업무 규칙(검증·담당자 규칙·승인·실행·중복 방지)은 모두 엔진에 있고, 여기서는 JSON 변환만 한다.
추출 결과는 승인 전까지 서버 메모리에만 두고, 승인하면 상태 저장소(SQLite)에 저장한 뒤 실행한다.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from onboarding_agent.actions.executor import execute_meeting
from onboarding_agent.actions.item_status import mark_cancelled, mark_done
from onboarding_agent.config import ConfigError, REPO_ROOT, get_settings
from onboarding_agent.meeting import review
from onboarding_agent.meeting.dates import format_due
from onboarding_agent.meeting.extract import extract_meeting
from onboarding_agent.meeting.loader import MeetingInputError, load_meeting
from onboarding_agent.meeting.polish import polish_overview
from onboarding_agent.meeting.models import ExtractionResult
from onboarding_agent.meeting.roster import Roster, RosterError, load_roster
from onboarding_agent.store import StoreUnavailable, get_store

SAMPLES_DIR = REPO_ROOT / "data" / "samples"
MAX_UPLOAD = 1_000_000
_CACHE_SIZE = 40
_cache: OrderedDict[str, ExtractionResult] = OrderedDict()   # 승인 전 추출 결과
_lock = threading.Lock()


class RowIn(BaseModel):
    item_id: str
    include: bool = True
    task: str = ""
    owner: str = review.OWNER_UNSET
    owner_ok: bool = False
    due_date: str | None = None
    due_time: str | None = None
    due_ok: bool = False


class ApproveIn(BaseModel):
    extraction_id: str
    rows: list[RowIn]
    summary: list[str] = []
    decisions: list[str] = []
    exclude_unconfirmed: bool = False
    execute: bool = True


class ItemsIn(BaseModel):
    item_ids: list[str]


def _roster(settings) -> Roster:
    try:
        return load_roster(settings.roster_path)
    except RosterError:
        return Roster()


def _ser_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    d, t = row.get("due_date"), row.get("due_time")
    out["due_date"] = d.isoformat() if isinstance(d, date) else None
    out["due_time"] = t.strftime("%H:%M") if isinstance(t, time) else None
    return out


def _from_client(original: list[dict[str, Any]], incoming: list[RowIn]) -> list[dict[str, Any]]:
    """클라이언트가 보낸 편집값을 원래 행 위에 얹는다. 모르는 item_id(=사용자가 추가한 행)는 새 행으로 만든다."""
    by_id = {r["item_id"]: r for r in original}
    rows: list[dict[str, Any]] = []
    for r in incoming:
        base = dict(by_id[r.item_id]) if r.item_id in by_id else {
            "evidence": "", "notes": review.USER_ADDED_NOTE, "due_text": "", "status": "", "needs_review": False,
            "item_id": r.item_id, "include": True, "task": "", "owner": review.OWNER_UNSET, "owner_ok": False,
            "due_date": None, "due_time": None, "due_ok": False,
        }
        base.update(
            include=bool(r.include),
            task=r.task,
            owner=review._coerce("owner", r.owner),
            owner_ok=bool(r.owner_ok),
            due_date=review._coerce("due_date", r.due_date),
            due_time=review._coerce("due_time", r.due_time),
            due_ok=bool(r.due_ok),
        )
        if base["owner"] == review.OWNER_UNSET:
            base["owner_ok"] = False
        if base["due_date"] is None:
            base["due_ok"] = False
            base["due_time"] = None
        rows.append(review.refresh_status(base))
    return rows


def _polish(result: ExtractionResult) -> ExtractionResult:
    """요약·결정사항·미결 사항의 말투만 공손하게 다듬는다. 할 일(담당자·기한)과 근거 인용은 건드리지 않는다."""
    m = result.meeting
    if result.extraction_path == "rule_based":          # 규칙 기반은 회의록 원문 문장이라 그대로 둔다
        return result
    out = polish_overview(m.summary, [d.text for d in m.decisions], m.open_issues)
    if not out.changed:
        return result
    decisions = [d.model_copy(update={"text": t}) for d, t in zip(m.decisions, out.decisions)]
    meeting = m.model_copy(update={"summary": out.summary, "decisions": decisions, "open_issues": out.open_issues})
    return result.model_copy(update={"meeting": meeting})


def _put(result: ExtractionResult) -> None:
    with _lock:
        _cache[result.meeting.meeting_id] = result
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def _ser_report(report) -> dict[str, Any]:
    return {
        "dry_run": report.dry_run,
        "failed": report.failed,
        "warnings": report.warnings,
        "slack_text": report.slack_message.text if report.slack_message else None,
        "slack_thread_reply": report.slack_thread_reply,
        "calendar_payloads": report.calendar_payloads,
        "results": [r.model_dump() for r in report.results],
    }


def register(api: APIRouter) -> None:
    @api.get("/meeting/samples")
    def samples():
        return sorted(p.name for p in SAMPLES_DIR.glob("*.txt")) if SAMPLES_DIR.exists() else []

    @api.get("/meeting/config")
    def config():
        s = get_settings()
        return {"dry_run": s.actions_dry_run, "provider": s.provider,
                "calendar_ready": bool(s.google_service_account_json and s.gcal_calendar_id),
                "slack_ready": bool(s.slack_bot_token and s.slack_channel_id)}

    @api.post("/meeting/extract")
    async def extract(file: UploadFile | None = File(None), sample: str | None = Form(None),
                      title: str | None = Form(None), meeting_date: str | None = Form(None)):
        if sample:
            path = SAMPLES_DIR / Path(sample).name          # 경로 이탈 방지: 파일 이름만 사용
            if not path.is_file():
                raise HTTPException(404, "샘플을 찾을 수 없어요.")
            data, filename = path.read_bytes(), path.name
        elif file is not None:
            data, filename = await file.read(), file.filename
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "파일이 너무 큽니다 (1MB 이하).")
        else:
            raise HTTPException(400, "회의록 파일을 올려 주세요.")
        try:
            settings = get_settings()
            loaded = load_meeting(data=data, filename=filename)
            when = date.fromisoformat(meeting_date) if meeting_date else (loaded.meeting_date or date.today())
            roster = _roster(settings)
            result = extract_meeting(loaded.text, title=(title or loaded.title_suggestion), meeting_date=when,
                                     source_filename=filename, roster=roster)
        except (MeetingInputError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        except ConfigError as exc:
            raise HTTPException(500, str(exc))
        result = _polish(result)
        _put(result)
        rows = review.rows_from_result(result)
        m = result.meeting
        return {
            "extraction_id": m.meeting_id, "title": m.title, "meeting_date": m.meeting_date.isoformat(),
            "summary": m.summary, "decisions": [d.text for d in m.decisions], "open_issues": m.open_issues,
            "rows": [_ser_row(r) for r in rows], "owner_options": review.owner_options(roster, rows),
            "path": result.extraction_path, "model": result.model_used, "fallback_used": result.fallback_used,
            "warnings": result.warnings, "injection_blocked": result.injection_blocked,
            "dry_run": settings.actions_dry_run,
        }

    @api.post("/meeting/approve")
    def approve(body: ApproveIn):
        with _lock:
            result = _cache.get(body.extraction_id)
        if result is None:
            raise HTTPException(404, "추출 결과가 만료됐어요. 회의록을 다시 올려 주세요.")
        settings = get_settings()
        try:
            store = get_store(settings)
        except StoreUnavailable as exc:
            raise HTTPException(503, str(exc))
        rows = _from_client(review.rows_from_result(result), body.rows)
        approval = review.approve_meeting(
            store, result, rows, summary=body.summary, decisions=body.decisions,
            exclude_unconfirmed=body.exclude_unconfirmed, roster=_roster(settings), now=datetime.now(settings.tz))
        if approval.meeting is None:
            raise HTTPException(422, " / ".join(approval.plan.problems))
        out: dict[str, Any] = {
            "meeting_id": approval.meeting.meeting_id, "approved": len(approval.plan.approve),
            "excluded": len(approval.plan.exclude), "report": None,
        }
        if body.execute:
            out["report"] = _ser_report(execute_meeting(approval.meeting.meeting_id, store=store, settings=settings))
        return out

    @api.post("/meeting/{meeting_id}/execute")
    def execute(meeting_id: str):
        settings = get_settings()
        store = get_store(settings)
        if store.get_meeting(meeting_id) is None:
            raise HTTPException(404, "회의를 찾을 수 없어요.")
        return _ser_report(execute_meeting(meeting_id, store=store, settings=settings))

    @api.get("/meeting/list")
    def list_meetings():
        settings = get_settings()
        store = get_store(settings)
        out = []
        for m in sorted(store.list_meetings(), key=lambda x: x.created_at, reverse=True)[:20]:
            items = store.list_items(meeting_id=m.meeting_id)
            out.append({
                "meeting_id": m.meeting_id, "title": m.title, "meeting_date": m.meeting_date.isoformat(),
                "items": [{
                    "item_id": i.item_id, "task": i.task, "owner": i.owner_name or review.OWNER_UNSET,
                    "due": format_due(i.due_date, i.due_time) if i.due_date else "", "status": i.status,
                    "calendar": bool(i.calendar_event_id), "slack": bool(i.slack_ts),
                } for i in items],
            })
        return out

    @api.post("/meeting/items/complete")
    def complete(body: ItemsIn):
        settings = get_settings()
        done = mark_done(get_store(settings), body.item_ids, datetime.now(settings.tz))
        return {"updated": len(done)}

    @api.post("/meeting/items/cancel")
    def cancel(body: ItemsIn):
        return {"updated": len(mark_cancelled(get_store(get_settings()), body.item_ids))}
