"""Shared Gemini client with a model fallback chain.

Models are tried in order: GEMINI_MODEL_PRIMARY, then GEMINI_MODEL_FALLBACKS.
Each model gets a few attempts with exponential backoff on transient errors
(429, 408, 5xx, timeouts, unusable output); other errors move straight to the
next model. A model that just failed is skipped for a while (simple circuit
breaker). When no model works, or FORCE_FALLBACK is on, LLMUnavailable is
raised so the caller can switch to its rule-based path.

Function calls returned by the model are never executed here. Callers decide
what to do with them; side effects must wait for user approval.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)

ATTEMPTS_PER_MODEL = 2
BACKOFF_INITIAL_SEC = 1.0
BACKOFF_MAX_SEC = 8.0
CIRCUIT_OPEN_SEC = 300.0


class LLMUnavailable(RuntimeError):
    """No model produced a usable answer; the caller should use its fallback."""


class LLMOutputError(ValueError):
    """The model answered, but the output cannot be used (empty, missing call, ...)."""


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, errors.APIError):
        code = exc.code or 0
        return code in (408, 429) or code >= 500
    return isinstance(exc, (httpx.TransportError, LLMOutputError, ValidationError))


def _describe(exc: BaseException) -> str:
    # Codes and types only: error bodies may echo request content.
    if isinstance(exc, errors.APIError):
        return f"{exc.code} {exc.status or ''}".strip()
    return type(exc).__name__


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        genai_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._genai_client = genai_client
        self._sleep = sleep
        self._clock = clock
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def available_models(self) -> list[str]:
        """Configured models whose circuit is closed, in try order."""
        now = self._clock()
        with self._lock:
            return [m for m in self.settings.gemini_models if self._open_until.get(m, 0.0) <= now]

    def run(self, call: Callable[[Any, str], T]) -> tuple[T, str]:
        """Run `call(genai_client, model_name)` across the model chain.

        Returns the call's result and the model that produced it.
        """
        settings = self.settings
        if settings.force_fallback:
            raise LLMUnavailable("FORCE_FALLBACK이 켜져 있어 LLM을 호출하지 않습니다.")
        if not settings.gemini_models:
            raise LLMUnavailable("GEMINI_MODEL_PRIMARY가 설정되지 않았습니다.")
        client = self._get_genai_client()
        models = self.available_models()
        if not models:
            raise LLMUnavailable("최근 실패한 모델만 남아 있어 잠시 LLM 호출을 건너뜁니다.")

        failures: list[str] = []
        for model in models:
            try:
                result = self._with_retries(lambda: call(client, model))
            except Exception as exc:  # any failure moves on to the next model
                self._trip(model)
                failures.append(f"{model}: {_describe(exc)}")
                logger.warning("LLM model %s failed: %s", model, _describe(exc))
                continue
            return result, model
        raise LLMUnavailable("모든 모델 호출이 실패했습니다 — " + "; ".join(failures))

    def generate_structured(
        self, prompt: str, schema: type[M], system: str | None = None
    ) -> tuple[M, str]:
        """JSON output validated against `schema`. Returns (result, model used)."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
        )

        def call(client: Any, model: str) -> M:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            if not response.text:
                raise LLMOutputError("empty response")
            return schema.model_validate_json(response.text)

        return self.run(call)

    def generate_with_tools(
        self,
        contents: Any,
        tools: Sequence[types.FunctionDeclaration],
        *,
        parse: Callable[[types.GenerateContentResponse], T],
        system: str | None = None,
        require_call: bool = False,
        allowed_function_names: Sequence[str] | None = None,
        temperature: float | None = None,
    ) -> tuple[T, str]:
        """One model turn with function declarations. Returns (parse(response), model used).

        `require_call=True` forces at least one function call (mode ANY);
        `allowed_function_names` narrows which ones and needs `require_call`.
        `parse` should raise LLMOutputError or ValidationError for unusable
        output, which counts as a transient failure.
        """
        if allowed_function_names and not require_call:
            raise ValueError("allowed_function_names requires require_call=True")
        mode = (
            types.FunctionCallingConfigMode.ANY
            if require_call
            else types.FunctionCallingConfigMode.AUTO
        )
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            tools=[types.Tool(function_declarations=list(tools))],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=mode,
                    allowed_function_names=list(allowed_function_names or []) or None,
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        def call(client: Any, model: str) -> T:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            return parse(response)

        return self.run(call)

    def _get_genai_client(self) -> Any:
        if self._genai_client is None:
            key = self.settings.gemini_api_key
            if key is None:
                raise LLMUnavailable("GEMINI_API_KEY가 설정되지 않았습니다.")
            self._genai_client = genai.Client(
                api_key=key.get_secret_value(),
                http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_sec * 1000)),
            )
        return self._genai_client

    def _with_retries(self, fn: Callable[[], T]) -> T:
        retrying = Retrying(
            stop=stop_after_attempt(ATTEMPTS_PER_MODEL),
            wait=wait_exponential(multiplier=BACKOFF_INITIAL_SEC, max=BACKOFF_MAX_SEC),
            retry=retry_if_exception(_is_transient),
            sleep=self._sleep,
            reraise=True,
        )
        return retrying(fn)

    def _trip(self, model: str) -> None:
        with self._lock:
            self._open_until[model] = self._clock() + CIRCUIT_OPEN_SEC


_default_client: LLMClient | None = None
_default_lock = threading.Lock()


def get_client() -> LLMClient:
    """Process-wide client, so circuit-breaker state survives Streamlit reruns."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = LLMClient()
        return _default_client


def reset_client() -> None:
    global _default_client
    with _default_lock:
        _default_client = None


def generate_structured(
    prompt: str, schema: type[M], system: str | None = None
) -> tuple[M, str]:
    return get_client().generate_structured(prompt, schema, system)


def generate_with_tools(
    contents: Any,
    tools: Sequence[types.FunctionDeclaration],
    *,
    parse: Callable[[types.GenerateContentResponse], T],
    system: str | None = None,
    require_call: bool = False,
    allowed_function_names: Sequence[str] | None = None,
    temperature: float | None = None,
) -> tuple[T, str]:
    return get_client().generate_with_tools(
        contents,
        tools,
        parse=parse,
        system=system,
        require_call=require_call,
        allowed_function_names=allowed_function_names,
        temperature=temperature,
    )
