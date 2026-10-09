"""회의록 텍스트 → 요약·결정사항·액션 아이템(담당자·기한) 구조화 추출 (OpenAI structured outputs)."""
import json
import re
from datetime import date

from backend import config

FALLBACK_MODELS = ["gpt-4o-mini", "gpt-4.1"]
MAX_CHARS = 60_000

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "summary", "decisions", "action_items", "open_questions"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["task", "owner", "due", "evidence"],
            "properties": {
                "task": {"type": "string"},
                "owner": {"type": ["string", "null"]},
                "due": {"type": ["string", "null"]},
                "evidence": {"type": "string"}}}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}

PROMPT = """너는 회의록을 정리하는 비서다. 입력은 회의 내용을 적은 텍스트다. 아래 규칙으로 JSON을 만든다.

- title: 회의 제목(없으면 내용에서 짧게 붙인다).
- summary: 핵심 논의 3~5줄.
- decisions: 회의에서 '확정된' 사항만. 논의만 되고 정해지지 않은 것은 넣지 않는다.
- action_items: 해야 할 일 목록. 각 항목에 대해
  - owner: 회의록에 담당자가 분명히 적힌 경우만 그 이름. 불분명하면 null. 추측 금지.
  - due: 기한이 적혀 있으면 YYYY-MM-DD로 변환(오늘은 {today}, {weekday}요일. '다음주 금요일' 같은 상대 표현은 이 날짜 기준으로 계산). 기한이 없으면 null. 추측 금지.
  - evidence: 그 항목의 근거가 된 회의록 원문 문장을 그대로 인용.
- 같은 발언에서 나온 같은 일은 하나의 항목으로 합친다(중복 금지).
- open_questions: 회의에서 결론이 나지 않았거나 담당자·기한이 정해지지 않아 확인이 필요한 사항.
회의록에 없는 내용을 만들어내지 않는다. 한국어로 쓴다."""

WEEKDAYS = "월화수목금토일"


def _clean_due(v):
    if not v:
        return None
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def analyze(text: str, today: date | None = None) -> dict:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
    text = text.strip()
    if not text:
        raise ValueError("회의록 내용이 비어 있습니다.")
    today = today or config.today()
    from openai import OpenAI
    client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=60, max_retries=1)
    msgs = [{"role": "system", "content": PROMPT.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()])},
            {"role": "user", "content": text[:MAX_CHARS]}]
    last = None
    for model in dict.fromkeys([config.OPENAI_MODEL, *FALLBACK_MODELS]):
        try:
            r = client.chat.completions.create(
                model=model, messages=msgs,
                response_format={"type": "json_schema", "json_schema": {"name": "meeting", "strict": True, "schema": SCHEMA}})
            data = json.loads(r.choices[0].message.content)
            break
        except Exception as e:  # 다음 모델로
            last = e
    else:
        raise RuntimeError(f"LLM 호출 실패: {type(last).__name__}") from last
    for it in data["action_items"]:
        it["due"] = _clean_due(it["due"])
        it["owner"] = (it["owner"] or "").strip() or None
    data["model"] = model
    data["truncated"] = len(text) > MAX_CHARS
    return data
