"""OpenAI-backed twin of LLMClient (same public methods, same model-chain behaviour).

The meeting engine only needs `run_tool_loop` and `generate_structured`. This
class keeps LLMClient's retry, model fallback and circuit breaker and swaps the
provider call underneath, so extraction, validation and rules stay unchanged.

Function calls are only recorded (never executed), exactly like the Gemini path.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from pydantic import ValidationError

from ..config import get_settings
from .client import (
    LLMClient,
    LLMOutputError,
    LLMUnavailable,
    M,
    T,
    ToolCallRecord,
    ToolLoopResult,
    _inline_schema,
)

logger = logging.getLogger(__name__)


def _is_transient(exc: BaseException) -> bool:
    try:
        import openai
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    return isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, httpx.TransportError, LLMOutputError, ValidationError))


def _describe(exc: BaseException) -> str:
    code = getattr(exc, "status_code", None)  # codes and types only: bodies may echo request content
    return f"{code} {type(exc).__name__}" if code else type(exc).__name__


def _tool_spec(declaration: Any) -> dict[str, Any]:
    """Gemini FunctionDeclaration (name, description, parameters_json_schema) -> OpenAI tool."""
    return {
        "type": "function",
        "function": {
            "name": declaration.name,
            "description": declaration.description or "",
            "parameters": declaration.parameters_json_schema or {"type": "object", "properties": {}},
        },
    }


class OpenAIClient(LLMClient):
    def available_models(self) -> list[str]:
        now = self._clock()
        with self._lock:
            return [m for m in self.settings.openai_models if self._open_until.get(m, 0.0) <= now]

    def run(self, call: Callable[[Any, str], T]) -> tuple[T, str]:
        settings = self.settings
        if settings.force_fallback:
            raise LLMUnavailable("FORCE_FALLBACK이 켜져 있어 LLM을 호출하지 않습니다.")
        if not settings.openai_models:
            raise LLMUnavailable("OPENAI_MODEL이 설정되지 않았습니다.")
        client = self._get_genai_client()
        models = self.available_models()
        if not models:
            raise LLMUnavailable("최근 실패한 모델만 남아 있어 잠시 LLM 호출을 건너뜁니다.")
        failures: list[str] = []
        for model in models:
            try:
                result = self._with_retries(lambda m=model: call(client, m))
            except Exception as exc:  # any failure moves on to the next model
                self._trip(model)
                failures.append(f"{model}: {_describe(exc)}")
                logger.warning("LLM model %s failed: %s", model, _describe(exc))
                continue
            return result, model
        raise LLMUnavailable("모든 모델 호출이 실패했습니다 — " + "; ".join(failures))

    def _with_retries(self, fn: Callable[[], T]) -> T:
        from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

        from .client import ATTEMPTS_PER_MODEL, BACKOFF_INITIAL_SEC, BACKOFF_MAX_SEC

        retrying = Retrying(
            stop=stop_after_attempt(ATTEMPTS_PER_MODEL),
            wait=wait_exponential(multiplier=BACKOFF_INITIAL_SEC, max=BACKOFF_MAX_SEC),
            retry=retry_if_exception(_is_transient),
            sleep=self._sleep,
            reraise=True,
        )
        return retrying(fn)

    def _get_genai_client(self) -> Any:  # name kept so LLMClient internals keep working
        if self._genai_client is None:
            key = self.settings.openai_api_key
            if key is None:
                raise LLMUnavailable("OPENAI_API_KEY가 설정되지 않았습니다.")
            from openai import OpenAI

            self._genai_client = OpenAI(
                api_key=key.get_secret_value(), timeout=self.settings.llm_timeout_sec, max_retries=0
            )
        return self._genai_client

    def generate_structured(self, prompt: str, schema: type[M], system: str | None = None) -> tuple[M, str]:
        json_schema = _inline_schema(schema.model_json_schema())
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]

        def call(client: Any, model: str) -> M:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": False},
                },
            )
            text = response.choices[0].message.content
            if not text:
                raise LLMOutputError("empty response")
            return schema.model_validate_json(text)

        return self.run(call)

    def run_tool_loop(
        self,
        prompt: str,
        tools: Sequence[Any],
        *,
        respond: Callable[[ToolCallRecord], dict[str, Any]],
        is_done: Callable[[ToolCallRecord], bool],
        max_turns: int,
        system: str | None = None,
        require_call: bool = True,
        allowed_function_names: Sequence[str] | None = None,
        temperature: float | None = None,
    ) -> tuple[ToolLoopResult, str]:
        allowed = set(allowed_function_names) if allowed_function_names else None
        specs = [_tool_spec(t) for t in tools if allowed is None or t.name in allowed]
        extra: dict[str, Any] = {} if temperature is None else {"temperature": temperature}

        def call(client: Any, model: str) -> ToolLoopResult:
            messages: list[dict[str, Any]] = ([{"role": "system", "content": system}] if system else [])
            messages.append({"role": "user", "content": prompt})
            records: list[ToolCallRecord] = []
            for turn in range(1, max_turns + 1):
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=specs,
                    tool_choice="required" if require_call else "auto",
                    **extra,
                )
                message = response.choices[0].message
                tool_calls = message.tool_calls or []
                if not tool_calls:
                    return ToolLoopResult(tuple(records), turn, "no_calls", message.content)
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                            for tc in tool_calls
                        ],
                    }
                )
                done = False
                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        args = args if isinstance(args, dict) else {}
                    except json.JSONDecodeError:
                        args = {}  # the engine's validation then rejects the call and reports it
                    record = ToolCallRecord(turn, tc.function.name or "", args)
                    records.append(record)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(respond(record), ensure_ascii=False, default=str)}
                    )
                    done = done or is_done(record)
                if done:
                    return ToolLoopResult(tuple(records), turn, "done")
            return ToolLoopResult(tuple(records), max_turns, "max_turns")

        return self.run(call)
