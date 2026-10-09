"""연가 신청 액션: 초안 작성(슬롯 필링 + 프로필 자동 채움) → 제출(가상 결재 API)."""
import re
from datetime import date, timedelta

from backend import config
from backend.actions import calendar_kr
from backend.state import store

LEAVE_TYPES = {"종일": 1.0, "오전반차": 0.5, "오후반차": 0.5}

_WD = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}


def parse_korean_date(text: str, today: date | None = None) -> date | None:
    """'다음주 금요일', '내일', '10/20', '10월 20일', '2026-10-20' 등을 date로."""
    today = today or config.today()
    if m := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text) or re.search(r"\b(\d{1,2})/(\d{1,2})\b", text):
        d = date(today.year, int(m[1]), int(m[2]))
        return d if d >= today else date(today.year + 1, d.month, d.day)
    if "모레" in text:
        return today + timedelta(days=2)
    if "내일" in text:
        return today + timedelta(days=1)
    if m := re.search(r"(이번\s*주|다음\s*주|다다음\s*주)?\s*([월화수목금토일])요일", text):
        wd = _WD[m[2]]
        monday = today - timedelta(days=today.weekday())
        prefix = (m[1] or "").replace(" ", "")
        weeks = {"이번주": 0, "다음주": 1, "다다음주": 2}.get(prefix)
        if weeks is not None:
            return monday + timedelta(days=7 * weeks + wd)
        d = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        return d
    return None


def parse_leave_type(text: str) -> str | None:
    if "오전" in text and "반" in text:
        return "오전반차"
    if "오후" in text and "반" in text:
        return "오후반차"
    if "반차" in text or "반일" in text:
        return "오후반차"  # 모호하면 에이전트가 되묻도록 호출측에서 처리
    if any(k in text for k in ("종일", "하루", "전일", "온종일")):
        return "종일"
    return None


def _validate(emp_id: str, leave_date: str, leave_type: str) -> tuple[dict | None, dict | None]:
    """(에러, 프로필) 반환."""
    if leave_type not in LEAVE_TYPES:
        return {"error": f"leave_type은 {list(LEAVE_TYPES)} 중 하나여야 합니다."}, None
    d = date.fromisoformat(leave_date)
    if d.weekday() >= 5:
        return {"error": f"{leave_date}은(는) 주말이라 연가가 필요하지 않습니다."}, None
    if name := calendar_kr.holiday_name(d):
        return {"error": f"{leave_date}은(는) 공휴일({name})이라 연가가 필요하지 않습니다."}, None
    if d < config.today():
        return {"error": "과거 날짜에는 연가를 신청할 수 없습니다."}, None
    remaining = store.leave_remaining(emp_id)
    if LEAVE_TYPES[leave_type] > remaining:
        return {"error": f"잔여 연가({remaining}일)가 부족합니다."}, None
    return None, store.get_employee(emp_id)


def fill_leave_form(emp_id: str, leave_date: str, leave_type: str, reason: str = "개인 사유") -> dict:
    """복무시스템의 연가 신청 폼을 자동 입력해 둔다(상신 전 상태). 이름·사번·소속·결재선은 프로필에서 자동으로 채운다.
    실제 상신은 사용자가 시스템 화면에서 확인 후 직접 누른다.

    Args:
        emp_id: 사번
        leave_date: ISO 날짜 (YYYY-MM-DD)
        leave_type: '종일' | '오전반차' | '오후반차'
        reason: 사유
    """
    err, p = _validate(emp_id, leave_date, leave_type)
    if err:
        return err
    days = LEAVE_TYPES[leave_type]
    remaining = store.leave_remaining(emp_id)
    form = {
        "신청자": p["name"], "사번": p["emp_id"], "소속": f'{p["org"]} {p["dept"]}', "직급": p["rank"],
        "구분": "연가", "일자": leave_date, "시간구분": leave_type, "일수": days, "사유": reason,
        "잔여연가(신청 전)": remaining, "잔여연가(승인 후)": remaining - days,
        "결재라인": " → ".join(f'{a["role"]} {a["name"]}' for a in p["approvers"]),
    }
    form["req_id"] = store.save_leave_request(emp_id, form, status="FORM_FILLED")
    return form


def submit_form(req_id: int, edits: dict | None = None) -> dict:
    """[모의 복무시스템] 사용자가 '상신'을 누른 시점. 화면에서 수정한 값(edits: 일자/시간구분/사유)을 반영해
    다시 검증한 뒤, 결재선에 진입하고 잔여 연가를 차감한다."""
    r = store.get_leave_request(req_id)
    if r["status"] != "FORM_FILLED" or r["payload"].get("구분") != "연가":
        return {"error": f'상신할 수 없는 문서입니다 (상태: {r["status"]}).'}
    pl = dict(r["payload"])
    if edits:
        pl.update({k: v for k, v in edits.items() if k in ("일자", "시간구분", "사유")})
        pl["일수"] = LEAVE_TYPES.get(pl["시간구분"], pl["일수"])
        remaining = store.leave_remaining(r["emp_id"])
        pl["잔여연가(신청 전)"], pl["잔여연가(승인 후)"] = remaining, remaining - pl["일수"]
    err, _ = _validate(r["emp_id"], pl["일자"], pl["시간구분"])
    if err:
        return err
    store.update_request_payload(req_id, pl)
    store.update_leave_status(req_id, "PENDING_APPROVAL")
    store.adjust_leave_used(r["emp_id"], pl["일수"])
    return {"req_id": req_id, "문서번호": f"MOCK-{config.today():%Y%m%d}-{req_id:04d}",
            "현재결재자": pl["결재라인"].split(" → ")[0]}


def approve(req_id: int) -> dict:
    """[모의 복무시스템 · 결재자 시연] 승인 처리."""
    r = store.get_leave_request(req_id)
    if r["status"] != "PENDING_APPROVAL":
        return {"error": "결재 대기 문서가 아닙니다."}
    store.update_leave_status(req_id, "APPROVED")
    return {"req_id": req_id, "status": "APPROVED"}


def suggest_leave_dates(emp_id: str) -> list[dict]:
    """연가 1일로 가장 길게 쉴 수 있는 날짜를 추천한다 (주말·공휴일 연계)."""
    p = store.get_employee(emp_id)
    return calendar_kr.suggest_dates(config.today(), hire_date=date.fromisoformat(p["hire_date"]))
