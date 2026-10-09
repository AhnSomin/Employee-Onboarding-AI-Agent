"""비품 신청 액션: 초안 → 제출(mock). 제출 시 로드맵의 '비품 신청' 항목을 자동 완료 처리한다."""
import re

from backend import config
from backend.state import store

CATALOG = {  # 데모용 지급 가능 품목 (실제로는 부서 비품 목록 연동)
    "볼펜": "개", "노트": "권", "키보드": "개", "마우스": "개", "모니터": "대",
    "파일철": "개", "포스트잇": "개", "명함": "박스", "이어폰": "개",
}
SUPPLY_TASK_ID = "c6"


def parse_item(text: str) -> tuple[str | None, int | None]:
    item = next((k for k in CATALOG if k in text), None)
    m = re.search(r"(\d+)\s*(?:개|대|권|자루|박스|세트)?", text)
    qty = int(m[1]) if m and item else None
    return item, qty


def fill_supply_form(emp_id: str, item: str, quantity: int = 1, reason: str = "업무용") -> dict:
    """복무시스템의 비품 신청 폼을 자동 입력해 둔다(상신 전). 신청자·소속·결재라인은 프로필에서 채운다.
    상신은 사용자가 시스템 화면에서 직접 한다.

    Args:
        emp_id: 사번
        item: 품목명 (볼펜, 노트, 키보드, 마우스, 모니터, 파일철, 포스트잇, 명함, 이어폰)
        quantity: 수량
        reason: 사유
    """
    if item not in CATALOG:
        return {"error": f"'{item}'은(는) 신청 가능 품목이 아닙니다. 가능 품목: {', '.join(CATALOG)}"}
    if not 1 <= quantity <= 20:
        return {"error": "수량은 1~20개 범위로 신청할 수 있습니다."}
    p = store.get_employee(emp_id)
    draft = {
        "신청자": p["name"], "사번": p["emp_id"], "소속": f'{p["org"]} {p["dept"]}',
        "구분": "비품", "품목": item, "수량": f"{quantity}{CATALOG[item]}", "사유": reason,
        "결재라인": " → ".join(f'{a["role"]} {a["name"]}' for a in p["approvers"][:1]),  # 비품은 과장 전결
    }
    draft["req_id"] = store.save_leave_request(emp_id, draft, status="FORM_FILLED")
    return draft


def submit_form(req_id: int, edits: dict | None = None) -> dict:
    """[모의 복무시스템] 사용자가 '상신'을 누른 시점. 결재선에 진입하고 로드맵 '비품 신청' 항목을 자동 완료한다."""
    r = store.get_leave_request(req_id)
    if r["status"] != "FORM_FILLED" or r["payload"].get("구분") != "비품":
        return {"error": f'상신할 수 없는 문서입니다 (상태: {r["status"]}).'}
    if edits:  # 화면에서 수정한 값(품목/수량/사유) 반영 후 재검증
        item = edits.get("품목", r["payload"]["품목"])
        qty = int(edits.get("수량", 1))
        if item not in CATALOG or not 1 <= qty <= 20:
            return {"error": "품목 또는 수량(1~20)이 올바르지 않습니다."}
        r["payload"].update({"품목": item, "수량": f"{qty}{CATALOG[item]}", "사유": edits.get("사유", r["payload"]["사유"])})
        store.update_request_payload(req_id, r["payload"])
    store.update_leave_status(req_id, "PENDING_APPROVAL")
    store.set_task_done(r["emp_id"], SUPPLY_TASK_ID)
    done, total = store.progress(r["emp_id"])
    return {"req_id": req_id, "문서번호": f"MOCK-S{config.today():%Y%m%d}-{req_id:04d}",
            "현재결재자": r["payload"]["결재라인"], "roadmap": f"온보딩 '비품 신청' 항목 자동 완료 ({done}/{total})"}
