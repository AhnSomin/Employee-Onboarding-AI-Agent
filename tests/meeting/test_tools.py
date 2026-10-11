from onboarding_agent.llm.client import ToolCallRecord, ToolLoopResult
from onboarding_agent.meeting.tools import (
    FINISH,
    OVERVIEW,
    PROPOSE,
    TOOLS,
    collect,
    respond,
)

OVERVIEW_ARGS = {"summary": ["요약"], "decisions": [{"text": "결정", "evidence_quote": "근거"}]}
ITEM_ARGS = {"task": "자료 정리하기", "owner_name": "김민준", "evidence_quote": "자료는 김민준이 정리"}


def rec(name, args, turn=1):
    return ToolCallRecord(turn, name, args)


def test_tools_are_declared_without_refs_or_defaults():
    names = [t.name for t in TOOLS]
    assert names == [OVERVIEW, PROPOSE, FINISH]
    for tool in TOOLS:
        schema = str(tool.parameters_json_schema)
        assert "$ref" not in schema and "'default'" not in schema


def test_respond_is_pure_and_explains_rejections():
    assert respond(rec(PROPOSE, ITEM_ARGS)) == {"result": "recorded"}
    assert respond(rec(FINISH, {"item_count": 1})) == {"result": "finished"}
    rejected = respond(rec(PROPOSE, {"task": "x"}))
    assert rejected["result"] == "rejected"
    assert "evidence_quote" in rejected["reason"]
    assert respond(rec(FINISH, {"item_count": -1}))["result"] == "rejected"
    assert respond(rec("create_calendar_event", {}))["result"] == "rejected"
    assert respond(rec(PROPOSE, {**ITEM_ARGS, "task": "  "}))["result"] == "rejected"


def test_collect_keeps_first_overview_and_finish():
    out = collect(
        [
            rec(OVERVIEW, OVERVIEW_ARGS),
            rec(OVERVIEW, {"summary": ["다른 요약"]}),
            rec(PROPOSE, ITEM_ARGS),
            rec(FINISH, {"item_count": 1}),
            rec(FINISH, {"item_count": 5}),
        ]
    )
    assert out.overview.summary == ["요약"]
    assert out.finish_count == 1
    assert out.fallback_reasons() == []
    assert len([w for w in out.warnings if "여러 번" in w]) == 2


def test_collect_merges_duplicates_filling_missing_fields():
    out = collect(
        [
            rec(PROPOSE, ITEM_ARGS),
            rec(PROPOSE, {**ITEM_ARGS, "task": "자료  정리하기", "co_owners": ["이서연"]}, turn=2),
        ]
    )
    assert len(out.items) == 1
    assert out.items[0].co_owners == ["이서연"]
    assert out.merged_duplicates == 1


def test_fallback_reasons_cover_each_case():
    empty = collect([])
    assert len(empty.fallback_reasons()) == 3
    no_finish = collect([rec(OVERVIEW, OVERVIEW_ARGS), rec(PROPOSE, ITEM_ARGS)])
    assert no_finish.fallback_reasons() == ["4턴 안에 finish_extraction 호출이 오지 않았습니다"]


def test_summary_counts_every_call():
    calls = [rec(OVERVIEW, OVERVIEW_ARGS), rec(PROPOSE, {"bad": 1}), rec(FINISH, {"item_count": 0}, turn=2)]
    out = collect(calls)
    summary = out.summary(ToolLoopResult(tuple(calls), 2, "done"))
    assert summary.turns == 2
    assert summary.calls == {OVERVIEW: 1, PROPOSE: 1, FINISH: 1}
    assert summary.rejected == 1
    assert summary.finished
