"""Call budget for regulation Q&A (instruction v2, section 3), kept across runs.

Every request that reaches Gemini is counted, retries and fallbacks
included: a CountingGenai wraps the SDK client and records each
`generate_content` and `embed_content` call in a JSONL log before and after
it runs. A call that would pass a cap raises BudgetExceeded instead of being
sent. A 429 (quota) halts that kind of call for good, so nothing retries into
a spent quota; the halt is in the log until a person clears it.

Totals are per budget session (from the `since` time in budget.yaml) for the
caps, and also kept over all time for reporting. Document token counts are
estimates (see chunker.estimate_tokens).
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# Instruction v2 section 3 defaults; a task can set its own in data/regulations/budget.yaml.
CAPS = {"generate": 12, "embed_query": 30, "embed_doc_tokens": 120_000}
GENERATE_PLAN = {"final_eval": 5, "app_question": 1, "regenerate": 2, "tool_mode": 3, "spare": 1}


class BudgetExceeded(RuntimeError):
    """The call would pass its cap, or that kind of call was halted after a 429."""


@dataclass(frozen=True)
class UsageRecord:
    at: str
    kind: str  # generate | embed_query | embed_doc
    purpose: str
    model: str
    requests: int
    est_tokens: int
    status: str  # ok | error:<code> | quota
    inputs: int = 1


class UsageMeter:
    """Counts requests per budget session (records at or after `since`) and over all time.

    Caps and the per-purpose generation plan come from data/regulations/budget.yaml when
    it exists (see `from_budget_file`), otherwise from the instruction defaults above.
    """

    def __init__(
        self,
        path: Path,
        caps: dict[str, int] | None = None,
        enforce: bool = True,
        since: str | None = None,
        plan: dict[str, int] | None = None,
        session: str | None = None,
    ) -> None:
        self.path = path
        self.caps = dict(CAPS if caps is None else caps)
        self.plan = dict(GENERATE_PLAN if plan is None else plan)
        self.enforce = enforce  # False: log every call but block none
        self.since = datetime.fromisoformat(since) if since else None
        self.session = session
        self._lock = threading.Lock()

    @classmethod
    def from_budget_file(cls, path: Path, budget_file: Path, enforce: bool = True) -> UsageMeter:
        if not budget_file.is_file():
            return cls(path, enforce=enforce)
        data = yaml.safe_load(budget_file.read_text(encoding="utf-8")) or {}
        return cls(
            path,
            caps=data.get("caps"),
            enforce=enforce,
            since=str(data["since"]) if data.get("since") else None,
            plan=data.get("generate_plan"),
            session=data.get("session"),
        )

    def records(self, lifetime: bool = False) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lifetime or self.since is None:
            return rows
        return [r for r in rows if datetime.fromisoformat(r["at"]) >= self.since]

    def totals(self, lifetime: bool = False) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for record in self.records(lifetime):
            if record["kind"] == "embed_doc":
                counts["embed_doc_tokens"] += record["est_tokens"]
                counts["embed_doc_requests"] += record["requests"]
            else:
                counts[record["kind"]] += record["requests"]
        return dict(counts)

    def by_purpose(self, kind: str = "generate", lifetime: bool = False) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for record in self.records(lifetime):
            if record["kind"] == kind:
                counts[record["purpose"]] += record["requests"]
        return dict(counts)

    def halted(self, kind: str) -> bool:
        return any(r["kind"] == kind and r["status"] == "quota" for r in self.records())

    def check(self, kind: str, amount: int = 1, est_tokens: int = 0, purpose: str | None = None) -> None:
        if not self.enforce:
            return
        if self.halted(kind):
            raise BudgetExceeded(f"{kind}: 한도 초과(429) 이후 호출을 멈췄습니다.")
        totals = self.totals()
        if kind == "embed_doc":
            if totals.get("embed_doc_tokens", 0) + est_tokens > self.caps["embed_doc_tokens"]:
                raise BudgetExceeded("문서 임베딩 입력이 예산(추정 토큰)을 넘습니다.")
            return
        if totals.get(kind, 0) + amount > self.caps[kind]:
            raise BudgetExceeded(f"{kind}: 예산 {self.caps[kind]}회를 넘습니다.")
        if kind == "generate" and purpose in self.plan:
            used = self.by_purpose("generate").get(purpose, 0)
            if used + amount > self.plan[purpose]:
                raise BudgetExceeded(f"생성 '{purpose}' 몫 {self.plan[purpose]}회를 다 썼습니다.")

    def record(self, record: UsageRecord) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")


def _status(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code == 429:
        return "quota"
    return f"error:{code or type(exc).__name__}"


class _CountingModels:
    def __init__(self, owner: CountingGenai) -> None:
        self._owner = owner

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        owner = self._owner
        owner.meter.check("generate", purpose=owner.purpose)
        try:
            response = owner.real.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:
            owner.meter.record(UsageRecord(_now(), "generate", owner.purpose, model, 1, 0, _status(exc)))
            raise
        owner.meter.record(UsageRecord(_now(), "generate", owner.purpose, model, 1, 0, "ok"))
        owner.generate_calls += 1
        return response

    def embed_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        owner = self._owner
        texts = contents if isinstance(contents, list) else [contents]
        kind = "embed_query" if owner.purpose.startswith("query") else "embed_doc"
        est = sum(len(t) for t in texts if isinstance(t, str))
        owner.meter.check(kind, 1, est)
        try:
            response = owner.real.models.embed_content(model=model, contents=contents, config=config)
        except Exception as exc:
            owner.meter.record(UsageRecord(_now(), kind, owner.purpose, model, 1, est, _status(exc), len(texts)))
            raise
        owner.meter.record(UsageRecord(_now(), kind, owner.purpose, model, 1, est, "ok", len(texts)))
        return response


class CountingGenai:
    """Stands in for google.genai.Client: same `models` calls, each one counted."""

    def __init__(self, real: Any, meter: UsageMeter, purpose: str = "spare") -> None:
        self.real = real
        self.meter = meter
        self.purpose = purpose
        self.generate_calls = 0
        self.models = _CountingModels(self)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
