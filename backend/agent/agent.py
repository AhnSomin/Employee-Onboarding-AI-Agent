"""대화 에이전트. OPENAI_API_KEY가 있으면 function calling, 없으면 규칙 기반 폴백(데모/테스트용)."""
import inspect
import json
import re
import time

from backend import config
from backend.actions import leave, supply
from backend.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_WEB
from backend.agent.tools import build_tools
from backend.state import store

WEEKDAYS = "월화수목금토일"
FALLBACK_MODELS = ["gpt-4o-mini", "gpt-4.1"]
MAX_TOOL_ROUNDS = 6
WEB_TOOLS = {"get_my_profile", "lookup_law", "escalate_to_hr", "get_roadmap", "mark_task_done"}
_JSON_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def tool_schemas(fns) -> list[dict]:
    """파이썬 함수의 시그니처·docstring으로 OpenAI tools 스키마를 만든다."""
    out = []
    for fn in fns:
        doc = inspect.getdoc(fn) or ""
        head, _, args = doc.partition("Args:")
        descs = dict(re.findall(r"^\s*(\w+):\s*(.+)$", args, re.M))
        props, req = {}, []
        for name, p in inspect.signature(fn).parameters.items():
            props[name] = {"type": _JSON_TYPES.get(getattr(p.annotation, "__name__", ""), "string")}
            if name in descs:
                props[name]["description"] = descs[name]
            if p.default is inspect.Parameter.empty:
                req.append(name)
        out.append({"type": "function", "function": {
            "name": fn.__name__, "description": head.strip(),
            "parameters": {"type": "object", "properties": props, "required": req}}})
    return out



def format_leave_form(f: dict) -> str:
    keys = ["신청자", "사번", "소속", "구분", "일자", "시간구분", "일수", "사유", "잔여연가(승인 후)", "결재라인"]
    rows = "\n".join(f"| {k} | {f[k]} |" for k in keys)
    return (f"✍️ 복무시스템 연가 신청 폼을 **자동 입력**해 뒀어요.\n\n| 항목 | 내용 |\n|---|---|\n{rows}\n\n"
            "👉 왼쪽 메뉴의 **모의 복무시스템** 페이지에서 내용을 확인하고 **상신**을 눌러주세요.")


def format_suggestions(sugs: list[dict]) -> str:
    lines = "\n".join(f"- **{s['date']}({s['요일']})** — {s['이유']}" for s in sugs)
    return f"📅 이런 날이 효율적이에요:\n\n{lines}\n\n원하는 날짜를 말해주세요."


class OnboardingAgent:
    def __init__(self, emp_id: str, profile: str = "full"):
        self.emp_id = emp_id
        self.profile = profile  # 'web': 규정 Q&A·로드맵 도구만, 웹용 프롬프트
        self.ctx = {"last_user_msg": "", "escalations": []}
        self.tools = build_tools(emp_id, self.ctx, only=WEB_TOOLS if profile == "web" else None)
        self.slots: dict = {}          # 폴백용 슬롯 상태
        self._client = None
        self.degraded = False
        if config.OPENAI_API_KEY:
            self._init_llm()

    @property
    def mode(self) -> str:
        if self._client:
            return "규칙 기반 (LLM 장애)" if self.degraded else f"openai · {self._models[self._model_idx]}"
        return "rule-based"

    # ---------- OpenAI ----------
    def _init_llm(self):
        from openai import OpenAI
        self._client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=30, max_retries=1)
        self._models = list(dict.fromkeys([config.OPENAI_MODEL, *FALLBACK_MODELS]))
        self._model_idx = 0
        self._tools_spec = tool_schemas(self.tools.values())
        t = config.today()
        self._messages = [{"role": "system", "content": (SYSTEM_PROMPT_WEB if self.profile == "web" else SYSTEM_PROMPT).format(
            today=t.isoformat(), weekday=WEEKDAYS[t.weekday()])}]

    def _run_llm(self, msgs: list[dict]) -> str:
        """도구 호출 루프. msgs를 제자리에서 늘린다."""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self._client.chat.completions.create(
                model=self._models[self._model_idx], messages=msgs, tools=self._tools_spec)
            m = resp.choices[0].message
            if not m.tool_calls:
                msgs.append({"role": "assistant", "content": m.content or ""})
                return m.content or ""
            msgs.append({"role": "assistant", "content": m.content, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in m.tool_calls]})
            for c in m.tool_calls:
                try:
                    result = self.tools[c.function.name](**json.loads(c.function.arguments or "{}"))
                except Exception as e:  # 잘못된 인자 등은 모델에게 알려 스스로 고치게 함
                    result = {"error": f"{type(e).__name__}: {e}"}
                msgs.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(result, ensure_ascii=False, default=str)})
        return "처리 중 문제가 생겼어요. 다시 한 번 말씀해 주세요."

    def _ask_llm(self, msg: str) -> str | None:
        """일시 장애 시 다음 모델로 전환해 재시도. 전부 실패하면 None. 성공한 턴만 이력에 반영."""
        for _ in range(len(self._models) * 2):
            msgs = self._messages + [{"role": "user", "content": msg}]
            try:
                text = self._run_llm(msgs)
                self._messages, self.degraded = msgs, False
                return text
            except Exception:
                self._model_idx = (self._model_idx + 1) % len(self._models)
                time.sleep(1)
        return None

    # ---------- public ----------
    def chat(self, msg: str) -> str:
        self.ctx["last_user_msg"] = msg
        if self._client:
            if (text := self._ask_llm(msg)) is not None:
                return text
            self.degraded = True
        return self._rule_based(msg)

    # ---------- 규칙 기반 폴백 ----------
    def _rule_based(self, msg: str) -> str:
        if "item" in self.slots or re.search(r"비품|" + "|".join(supply.CATALOG), msg) and not self.slots:
            return self._supply_flow(msg)
        if not self.slots and re.search(r"추천|언제.*(쓰|좋)|언제가", msg):
            return format_suggestions(self.tools["suggest_leave_dates"]())
        if self.slots or re.search(r"연가|휴가|반차|쉬고|쉴", msg):
            return self._leave_flow(msg)
        if re.search(r"진행률|로드맵|체크리스트|할 일|할일", msg):
            return self._roadmap()
        if m := re.search(r"(.+?)(?:을|를)?\s*(?:했어|완료|끝냈|마쳤)", msg):
            for t in store.get_tasks(self.emp_id):
                if not t["done"] and any(w in t["title"] for w in re.findall(r"\w{2,}", m[1])):
                    r = self.tools["mark_task_done"](t["task_id"])
                    return f"👍 '{t['title']}' 완료 처리! 진행률 {r['done']}/{r['total']}"
        if re.search(r"규정|며칠|몇 ?일|일수|반차|복무", msg):
            arts = self.tools["lookup_law"](msg)["articles"]
            if arts:
                return "📚 관련 규정이에요:\n\n" + arts[0]
        self.tools["escalate_to_hr"](msg)
        return "정확한 답을 드리기 어려워요. 인사담당관실 담당자에게 물어볼게요 🙏"

    def _leave_flow(self, msg: str) -> str:
        if "date" not in self.slots and (d := leave.parse_korean_date(msg)):
            self.slots["date"] = d
        if "type" not in self.slots and (t := leave.parse_leave_type(msg)):
            self.slots["type"] = t
        if "date" not in self.slots:
            return "연가는 언제 쓰실 건가요? (예: 다음주 금요일, 10/20)"
        if "type" not in self.slots:
            d = self.slots["date"]
            return f"{d:%m/%d}({WEEKDAYS[d.weekday()]}) 연가네요. 종일인가요, 반차(오전/오후)인가요?"
        d, t = self.slots.pop("date"), self.slots.pop("type")
        f = self.tools["fill_leave_form"](d.isoformat(), t)
        if "error" in f:
            return "⚠️ " + f["error"]
        return format_leave_form(f)

    def _supply_flow(self, msg: str) -> str:
        item, qty = supply.parse_item(msg)
        if "item" not in self.slots and item:
            self.slots["item"] = item
        if "item" not in self.slots:
            return f"어떤 비품이 필요하세요? ({', '.join(supply.CATALOG)})"
        if "qty" not in self.slots:
            m = re.search(r"(\d+)", msg)
            if m:
                self.slots["qty"] = int(m[1])
            else:
                return f"**{self.slots['item']}**은(는) 몇 {supply.CATALOG[self.slots['item']]} 필요하세요?"
        it, q = self.slots.pop("item"), self.slots.pop("qty")
        f = self.tools["fill_supply_form"](it, q)
        if "error" in f:
            return "⚠️ " + f["error"]
        keys = ["신청자", "사번", "소속", "품목", "수량", "사유", "결재라인"]
        rows = "\n".join(f"| {k} | {f[k]} |" for k in keys)
        return (f"✍️ 복무시스템 비품 신청 폼을 **자동 입력**해 뒀어요.\n\n| 항목 | 내용 |\n|---|---|\n{rows}\n\n"
                "👉 왼쪽 메뉴의 **모의 복무시스템 > 비품 신청**에서 확인 후 **상신**을 눌러주세요. 상신하면 로드맵이 자동 완료돼요.")

    def _roadmap(self) -> str:
        done, total = store.progress(self.emp_id)
        lines = [f"{'✅' if t['done'] else '⬜'} {t['title']} (~{t['due_date']})" for t in store.get_tasks(self.emp_id)]
        return f"📍 온보딩 진행률 **{done}/{total}**\n\n" + "\n".join(lines)
