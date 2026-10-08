"""A real Gemini function-calling response (saved without HTTP headers) parses as expected."""

import json
from datetime import date

from google.genai import types

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.llm.client import ToolCallRecord
from onboarding_agent.meeting.roster import load_roster
from onboarding_agent.meeting.tools import collect
from onboarding_agent.meeting.validate import validate_extraction

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "gemini_function_calls_sample01.json"
SAMPLE = REPO_ROOT / "data" / "samples" / "01_structured_minutes.txt"


def test_real_response_folds_into_a_complete_extraction():
    response = types.GenerateContentResponse.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    calls = [ToolCallRecord(1, fc.name, dict(fc.args or {})) for fc in response.function_calls]
    collected = collect(calls)
    assert collected.fallback_reasons() == []
    assert collected.finish_count == len(collected.items) == 4

    validated = validate_extraction(
        collected.to_extraction(),
        meeting_text=SAMPLE.read_text(encoding="utf-8"),
        meeting_date=date(2026, 10, 8),
        meeting_id="m1",
        roster=load_roster(REPO_ROOT / "data" / "roster.yaml"),
    )
    assert all(not i.needs_review for i in validated.items)  # quotes are verbatim
    assert {i.owner_name for i in validated.items} == {"김민준", "이서연", "박지훈", "최유진"}
    assert all(i.owner_status == i.due_status == "confirmed" for i in validated.items)
