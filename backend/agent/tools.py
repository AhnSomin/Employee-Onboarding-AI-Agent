"""에이전트가 호출하는 도구 모음. emp_id를 클로저로 묶어 LLM이 타인 데이터를 건드리지 못하게 한다."""
from backend import config
from backend.actions import leave, supply
from backend.rag import index as rag
from backend.state import store

def build_tools(emp_id: str, ctx: dict, only: set[str] | None = None) -> dict:
    """ctx: {'last_user_msg': str, 'escalations': list}"""

    def get_my_profile() -> dict:
        """내 프로필(이름, 소속, 직급, 잔여 연가, 결재라인)을 조회한다."""
        p = store.get_employee(emp_id)
        return {**{k: p[k] for k in ("name", "org", "dept", "rank", "job", "hire_date", "approvers")},
                "leave_remaining": store.leave_remaining(emp_id)}

    def fill_leave_form(leave_date: str, leave_type: str, reason: str = "개인 사유") -> dict:
        """복무시스템의 연가 신청 폼을 자동 입력한다. 상신은 사용자가 시스템 화면에서 직접 한다 (너는 상신할 수 없다).

        Args:
            leave_date: 연가 일자 YYYY-MM-DD
            leave_type: '종일', '오전반차', '오후반차' 중 하나
            reason: 사유 (기본 '개인 사유')
        """
        return leave.fill_leave_form(emp_id, leave_date, leave_type, reason)

    def suggest_leave_dates() -> list:
        """연가 1일로 가장 길게 쉴 수 있는 날짜(주말·공휴일 연계)를 추천한다."""
        return leave.suggest_leave_dates(emp_id)

    def fill_supply_form(item: str, quantity: int = 1, reason: str = "업무용") -> dict:
        """복무시스템의 비품 신청 폼을 자동 입력한다. 상신은 사용자가 시스템 화면에서 직접 한다 (너는 상신할 수 없다).

        Args:
            item: 품목명 (볼펜, 노트, 키보드, 마우스, 모니터, 파일철, 포스트잇, 명함, 이어폰)
            quantity: 수량
            reason: 사유 (기본 '업무용')
        """
        return supply.fill_supply_form(emp_id, item, quantity, reason)

    def get_roadmap() -> dict:
        """내 온보딩 체크리스트와 진행률을 조회한다."""
        done, total = store.progress(emp_id)
        return {"done": done, "total": total, "tasks": store.get_tasks(emp_id)}

    def mark_task_done(task_id: str) -> dict:
        """온보딩 항목을 완료 처리한다 (task_id는 get_roadmap에서 확인)."""
        store.set_task_done(emp_id, task_id)
        done, total = store.progress(emp_id)
        return {"ok": True, "done": done, "total": total}

    def lookup_law(query: str) -> dict:
        """국가공무원 복무규정·공무원 여비 규정·공무원보수규정·국가데이터처 직제 시행규칙에서 관련 조문을 검색한다.

        Args:
            query: 검색할 질문 또는 법령 용어 (예: 반일 연가, 출장 숙박비)
        """
        arts = rag.format_hits(rag.search(query, k=5))
        if not arts:
            return {"articles": [], "note": "관련 조문을 찾지 못함. 질의어를 법령 용어로 바꿔 다시 검색하거나, 그래도 없으면 escalate_to_hr를 호출할 것. 기억으로 답하지 말 것."}
        return {"articles": arts, "note": "각 항목 맨 앞 [ ] 안이 법령명·조·항이다. 질문과 무관한 조문뿐이면 답하지 말고 escalate_to_hr를 호출할 것."}

    def escalate_to_hr(question: str) -> dict:
        """답변 근거가 부족할 때 인사담당관실 담당자에게 질문을 전달한다."""
        ctx.setdefault("escalations", []).append(question)
        return {"ok": True, "message": "담당자에게 전달됨"}

    fns = [get_my_profile, fill_leave_form, suggest_leave_dates,
           fill_supply_form, get_roadmap,
           mark_task_done, lookup_law, escalate_to_hr]
    return {f.__name__: f for f in fns if only is None or f.__name__ in only}
