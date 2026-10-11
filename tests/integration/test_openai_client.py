"""OpenAIClient: 팀원 엔진이 기대하는 인터페이스(run_tool_loop, generate_structured)를 OpenAI 응답으로 채운다."""
import json
from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel

from onboarding_agent.config import Settings
from onboarding_agent.llm.client import LLMUnavailable, ToolCallRecord
from onboarding_agent.llm.openai_client import OpenAIClient
from onboarding_agent.meeting.tools import FINISH, OVERVIEW, PROPOSE, TOOLS, is_finish, respond


class Out(BaseModel):
    x: int


def _tc(i, name, args):
    return NS(id=f"call_{i}", function=NS(name=name, arguments=json.dumps(args)))


def _resp(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


class FakeOpenAI:
    def __init__(self, responses):
        self._responses, self.calls = list(responses), []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(fake, **settings):
    s = Settings(openai_api_key="sk-test", **settings)
    return OpenAIClient(s, genai_client=fake, sleep=lambda _x: None)


def test_tool_loop_records_calls_and_stops_on_finish():
    fake = FakeOpenAI([
        _resp(tool_calls=[_tc(1, OVERVIEW, {"title": "t"}), _tc(2, PROPOSE, {"task": "x"})]),
        _resp(tool_calls=[_tc(3, FINISH, {"item_count": 1})]),
    ])
    loop, model = _client(fake).run_tool_loop(
        "회의록", TOOLS, respond=respond, is_done=is_finish, max_turns=4, system="sys",
        allowed_function_names=[OVERVIEW, PROPOSE, FINISH])
    assert model == "gpt-4.1-mini" and loop.stop_reason == "done" and loop.turns == 2
    assert [c.name for c in loop.calls] == [OVERVIEW, PROPOSE, FINISH]
    first = fake.calls[0]
    assert first["tool_choice"] == "required" and {t["function"]["name"] for t in first["tools"]} == {OVERVIEW, PROPOSE, FINISH}
    # 대화에는 매 턴의 도구 호출(assistant)과 그 결과(tool)가 순서대로 쌓인다 (같은 목록을 공유하므로 마지막 상태로 확인)
    roles = [m["role"] for m in fake.calls[-1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool", "assistant", "tool"]


def test_invalid_json_arguments_become_empty_args():
    bad = NS(id="c", function=NS(name=PROPOSE, arguments="{not json"))
    fake = FakeOpenAI([_resp(tool_calls=[bad]), _resp(tool_calls=[_tc(2, FINISH, {"item_count": 0})])])
    loop, _ = _client(fake).run_tool_loop("p", TOOLS, respond=respond, is_done=is_finish, max_turns=3)
    assert loop.calls[0] == ToolCallRecord(1, PROPOSE, {})


def test_falls_back_to_next_model_when_first_fails():
    fake = FakeOpenAI([RuntimeError("boom"), _resp(content='{"x": 1}')])   # 일시 오류가 아니면 재시도 없이 다음 모델로

    result, model = _client(fake).generate_structured("p", Out, "sys")
    assert result.x == 1 and model == "gpt-4o-mini"      # 기본 체인: gpt-4.1-mini 실패 → gpt-4o-mini
    assert fake.calls[-1]["response_format"]["type"] == "json_schema"


def test_unavailable_without_key_or_when_forced():
    with pytest.raises(LLMUnavailable):
        OpenAIClient(Settings()).generate_structured("p", Out)
    with pytest.raises(LLMUnavailable):
        _client(FakeOpenAI([]), force_fallback=True).generate_structured("p", Out)


def test_provider_resolution():
    assert Settings().provider == "gemini"
    assert Settings(openai_api_key="k").provider == "openai"
    assert Settings(openai_api_key="k", llm_provider="gemini").provider == "gemini"
