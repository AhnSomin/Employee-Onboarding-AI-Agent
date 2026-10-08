from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors, types
from pydantic import BaseModel

from onboarding_agent.config import Settings
from onboarding_agent.llm.client import (
    CIRCUIT_OPEN_SEC,
    LLMClient,
    LLMOutputError,
    LLMUnavailable,
)


class Answer(BaseModel):
    value: int


def api_error(code: int) -> errors.APIError:
    error_class = errors.ServerError if code >= 500 else errors.ClientError
    return error_class(code, {"error": {"code": code, "status": "TEST", "message": "test"}})


def text(payload: str | None) -> SimpleNamespace:
    return SimpleNamespace(text=payload, function_calls=None)


class FakeModels:
    """Stands in for genai.Client().models, replaying scripted outcomes per model."""

    def __init__(self, script):
        self.script = {model: list(outcomes) for model, outcomes in script.items()}
        self.calls: list[str] = []
        self.last_config = None

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        self.last_config = config
        outcome = self.script[model].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_client(script, **overrides):
    settings = Settings(
        gemini_api_key="test-key",
        gemini_model_primary="primary",
        gemini_model_fallbacks=["backup"],
        **overrides,
    )
    models = FakeModels(script)
    sleeps: list[float] = []
    clock = Clock()
    client = LLMClient(
        settings, genai_client=SimpleNamespace(models=models), sleep=sleeps.append, clock=clock
    )
    return client, models, sleeps, clock


def test_primary_success_reports_model():
    client, models, sleeps, _ = make_client({"primary": [text('{"value": 1}')]})
    assert client.generate_structured("q", Answer) == (Answer(value=1), "primary")
    assert models.calls == ["primary"]
    assert sleeps == []


def test_transient_errors_retry_with_backoff_then_fall_back():
    client, models, sleeps, _ = make_client(
        {"primary": [api_error(503), api_error(429)], "backup": [text('{"value": 2}')]}
    )
    assert client.generate_structured("q", Answer) == (Answer(value=2), "backup")
    assert models.calls == ["primary", "primary", "backup"]
    assert len(sleeps) == 1


def test_timeout_is_retried_on_same_model():
    client, models, _, _ = make_client(
        {"primary": [httpx.ReadTimeout("slow"), text('{"value": 3}')]}
    )
    assert client.generate_structured("q", Answer) == (Answer(value=3), "primary")
    assert models.calls == ["primary", "primary"]


def test_non_transient_error_moves_on_without_retry():
    client, models, sleeps, _ = make_client(
        {"primary": [api_error(404)], "backup": [text('{"value": 4}')]}
    )
    assert client.generate_structured("q", Answer) == (Answer(value=4), "backup")
    assert models.calls == ["primary", "backup"]
    assert sleeps == []


def test_unparseable_output_is_retried_then_falls_back():
    client, models, _, _ = make_client(
        {
            "primary": [text("not json"), text('{"value": "x"}')],
            "backup": [text('{"value": 5}')],
        }
    )
    assert client.generate_structured("q", Answer) == (Answer(value=5), "backup")
    assert models.calls == ["primary", "primary", "backup"]


def test_all_models_failing_raises_and_opens_circuit():
    client, models, _, clock = make_client(
        {"primary": [api_error(500)] * 2, "backup": [api_error(500)] * 2}
    )
    with pytest.raises(LLMUnavailable):
        client.generate_structured("q", Answer)
    assert models.calls == ["primary", "primary", "backup", "backup"]

    with pytest.raises(LLMUnavailable):
        client.generate_structured("q", Answer)
    assert len(models.calls) == 4  # both circuits open: no new calls

    clock.now += CIRCUIT_OPEN_SEC + 1
    models.script["primary"].append(text('{"value": 6}'))
    assert client.generate_structured("q", Answer) == (Answer(value=6), "primary")


def test_force_fallback_never_calls_a_model():
    client, models, _, _ = make_client({}, force_fallback=True)
    with pytest.raises(LLMUnavailable):
        client.generate_structured("q", Answer)
    assert models.calls == []


def test_missing_key_or_model_raises_unavailable():
    with pytest.raises(LLMUnavailable):
        LLMClient(Settings(gemini_model_primary="primary")).generate_structured("q", Answer)
    no_model = LLMClient(
        Settings(gemini_api_key="k"), genai_client=SimpleNamespace(models=FakeModels({}))
    )
    with pytest.raises(LLMUnavailable):
        no_model.generate_structured("q", Answer)


def test_generate_with_tools_forces_call_and_disables_auto_execution():
    call = types.FunctionCall(name="propose", args={"x": 1})
    client, models, _, _ = make_client(
        {"primary": [SimpleNamespace(text=None, function_calls=[call])]}
    )
    declaration = types.FunctionDeclaration(
        name="propose",
        description="test",
        parameters_json_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    calls, model = client.generate_with_tools(
        "minutes",
        [declaration],
        parse=lambda response: response.function_calls,
        require_call=True,
        allowed_function_names=["propose"],
    )
    assert model == "primary"
    assert calls[0].args == {"x": 1}
    calling = models.last_config.tool_config.function_calling_config
    assert calling.mode == types.FunctionCallingConfigMode.ANY
    assert calling.allowed_function_names == ["propose"]
    assert models.last_config.automatic_function_calling.disable is True
    assert models.last_config.tools[0].function_declarations[0].name == "propose"


def test_generate_with_tools_retries_when_parse_rejects_output():
    def parse(response):
        if not response.function_calls:
            raise LLMOutputError("no function call")
        return response.function_calls

    good = SimpleNamespace(text=None, function_calls=[types.FunctionCall(name="propose", args={})])
    client, models, _, _ = make_client({"primary": [text("plain answer"), good]})
    calls, model = client.generate_with_tools("minutes", [], parse=parse)
    assert model == "primary"
    assert len(calls) == 1
    assert models.calls == ["primary", "primary"]


def test_allowed_function_names_require_forced_call():
    client, _, _, _ = make_client({})
    with pytest.raises(ValueError):
        client.generate_with_tools("m", [], parse=lambda r: r, allowed_function_names=["x"])
