"""Retrieval, gating, answers, checks and tool mode (instruction v2, sections 7, 8.2 and 11)."""

import json

import pytest

from onboarding_agent.rag.answer import ConversationState, is_follow_up, regulation_tools
from onboarding_agent.rag.escalation import load_escalations
from onboarding_agent.rag.models import ModelAnswer
from onboarding_agent.rag.retrieve import Retriever
from onboarding_agent.rag.validate import validate_answer
from onboarding_agent.retrieval import tokenize

from .conftest import INJECTION, LAW, RULE, FakeLLM, make_index, make_service


class BrokenEmbedder:
    def __init__(self, config):
        self.config = config

    def embed_query(self, text):
        raise RuntimeError("network down")


def answer(points, conditions=(), status="answered", one_line="결론"):
    return {
        "status": status,
        "one_line": one_line,
        "points": [{"text": t, "evidence_ids": ids} for t, ids in points],
        "conditions": [{"text": t, "evidence_ids": ids} for t, ids in conditions],
    }


def test_tokenizer_keeps_article_numbers_whole():
    terms = tokenize("복무규정 제 15 조의 2 와 제18조에 따른 병가")
    assert "제15조의2" in terms and "제18조" in terms and "병가" in terms


def test_direct_article_lookup_and_reference_expansion(small_index):
    index, embedder = small_index
    retriever = Retriever(index, embedder, min_score=0.0)
    result = retriever.search("복무규정 제5조 내용이 뭐예요?", mode="vector")
    assert result.hits[0].chunk.chunk_id == "dec36728:a5" and result.hits[0].via == "direct"
    ref = retriever.search("당직총사령실의 비품은 무엇인가요?", mode="vector", top_k=1)
    assert ref.hits[0].chunk.chunk_id == "pmo2161:a3"
    assert any(h.chunk.chunk_id == "pmo2161:a2" and h.via == "ref" for h in ref.hits)


def test_embedding_failure_falls_back_to_bm25(small_index):
    index, embedder = small_index
    retriever = Retriever(index, BrokenEmbedder(embedder.config), min_score=0.5, glossary={"반차": ["반일", "연가"]})
    result = retriever.search("반차는 어떻게 계산해요?", mode="vector")
    assert result.degraded and result.mode == "bm25" and result.hits[0].chunk.chunk_id == "dec36728:a6"
    unrelated = retriever.search("주차장 이용 요금", mode="vector")
    assert unrelated.gated  # no content word of the question in the top hits


def test_below_min_score_escalates_without_calling_the_model(small_index, tmp_path):
    index, embedder = small_index
    llm = FakeLLM()
    service = make_service(index, embedder, llm, tmp_path, min_score=0.99)
    result = service.answer("주택자금 대출은 어떻게 받나요?", ConversationState())
    assert result.answer.status == "escalation_required" and llm.prompts == []
    pending = load_escalations(tmp_path / "pending.jsonl")
    assert pending[0]["delivered"] is False and pending[0]["question"].startswith("주택자금")


def test_answered_with_checks_and_proviso_warning(small_index, tmp_path):
    index, embedder = small_index
    llm = FakeLLM([answer([("점심시간은 낮 12시부터 오후 1시까지입니다(제4조).", ["dec36728:a4"])])])
    service = make_service(index, embedder, llm, tmp_path)
    result = service.answer("점심시간은 몇 시부터 몇 시까지인가요?", ConversationState())
    reply = result.answer
    assert reply.status == "answered" and reply.model_used == "fake-primary" and not reply.fallback_used
    assert reply.checks["근거 ID"] == "passed" and reply.checks["조문 표기"] == "passed"
    assert reply.checks["단서·예외"] == "warning"  # 제4조 has "다만" but no condition cites it
    assert "국가공무원 복무규정 시행 2026. 10. 2." in reply.basis_version
    assert result.cited_ids == ["dec36728:a4"] and result.cards[0].chunk.chunk_id == "dec36728:a4"
    assert "[dec36728:a4]" in llm.prompts[0] and "API" not in llm.prompts[0]


@pytest.mark.parametrize(
    ("bad", "failure"),
    [
        (answer([("병가는 연 60일입니다.", ["dec36728:a99"])]), "검색되지 않은 근거 ID"),
        (answer([("병가는 연 60일입니다(제9조).", ["dec36728:a5"])]), "조문 표기"),
        (answer([("병가는 연 90일입니다.", ["dec36728:a5"])]), "'90일'"),
        (answer([("병가는 연 60일입니다.", [])]), "근거가 없는 설명"),
        (answer([("「공무원연금법」에 따라 병가는 연 60일입니다.", ["dec36728:a5"])]), "문서명"),
    ],
)
def test_wrong_ids_articles_numbers_and_names_fail(small_index, bad, failure):
    index, _ = small_index
    retrieved = {c.chunk_id: c for c in index.chunks}
    titles = {d.doc_id: d.doc_title for d in index.docs.values()}
    validation = validate_answer(ModelAnswer.model_validate(bad), retrieved, titles)
    assert not validation.ok and any(failure in f for f in validation.failures)


def test_failed_check_regenerates_once_then_shows_source_cards_only(small_index, tmp_path):
    index, embedder = small_index
    wrong = answer([("병가는 연 90일입니다.", ["dec36728:a5"])])
    llm = FakeLLM([wrong, wrong])
    service = make_service(index, embedder, llm, tmp_path)
    result = service.answer("병가는 1년에 며칠인가요?", ConversationState())
    assert len(llm.prompts) == 2 and "검증에서 다음 이유로 거절" in llm.prompts[1]
    assert result.answer.status == "escalation_required" and result.answer.points == []
    assert result.cards and result.cited_ids == []
    assert result.escalation_id and "검증" in result.answer.escalation_reason

    fixed = FakeLLM([wrong, answer([("병가는 연 60일의 범위에서 승인됩니다.", ["dec36728:a5"])])])
    ok = make_service(index, embedder, fixed, tmp_path).answer("병가는 1년에 며칠인가요?", ConversationState())
    assert ok.answer.status == "answered" and len(ok.attempts) == 2


def test_clarification_and_period_outside_the_index(small_index, tmp_path):
    index, embedder = small_index
    clarify = {
        "status": "needs_clarification",
        "clarification_question": "임신 중이신가요?",
        "missing_conditions": ["임신 여부"],
    }
    llm = FakeLLM([clarify])
    service = make_service(index, embedder, llm, tmp_path)
    result = service.answer("저도 쓸 수 있나요? 병가요", ConversationState())
    assert result.answer.status == "needs_clarification" and result.answer.missing_conditions == ["임신 여부"]
    past = service.answer("2015년 기준 병가는 며칠이었나요?", ConversationState())
    assert past.answer.status == "source_unavailable" and "2015년" in past.answer.escalation_reason
    assert len(llm.prompts) == 1  # no model call for the period question


def test_follow_up_keeps_conditions_and_previous_question_not_answers(small_index, tmp_path):
    index, embedder = small_index
    llm = FakeLLM(
        [
            answer([("병가는 연 60일의 범위입니다.", ["dec36728:a5"])]),
            answer([("공무상 질병은 연 180일의 범위입니다.", ["dec36728:a5"])]),
        ]
    )
    service = make_service(index, embedder, llm, tmp_path)
    state = ConversationState()
    service.answer("시간선택제 공무원인데 병가는 며칠인가요?", state)
    assert is_follow_up("그럼 공무상 질병이면요?")
    result = service.answer("그럼 공무상 질병이면요?", state)
    assert result.retrieval.query.startswith("시간선택제 공무원인데 병가는 며칠인가요?")
    assert "대상: 시간선택제" in llm.prompts[1] and "연 60일의 범위입니다" not in llm.prompts[1]
    for number in range(6):
        state.add(f"질문 {number}")
    assert state.conditions == [] and len(state.questions) == 5  # older than five turns


def test_repeating_the_users_condition_value_is_a_warning_not_a_regeneration(small_index, tmp_path):
    index, embedder = small_index
    llm = FakeLLM(
        [
            answer([("재직 3년이어도 병가는 연 60일의 범위에서 승인할 수 있습니다.", ["dec36728:a5"])]),
            answer([("병가가 8일이면 진단서가 필요합니다.", ["dec36728:a5"])]),
            answer([("병가가 8일이면 진단서가 필요합니다.", ["dec36728:a5"])]),
        ]
    )
    service = make_service(index, embedder, llm, tmp_path)
    state = ConversationState()
    result = service.answer("재직 3년인데 병가는 며칠까지 쓸 수 있나요?", state)
    assert result.answer.status == "answered" and len(llm.prompts) == 1
    assert result.answer.checks["수치·날짜"] == "warning"
    assert any("조건 값" in w for w in result.warnings)
    # "8일" was said by the user, but it is not a condition value and days are a conclusion unit
    result = service.answer("그럼 8일이면요?", state)
    assert result.answer.status == "escalation_required" and len(llm.prompts) == 3


def test_numbers_match_whole_numbers_and_a_restated_condition_stays(small_index, tmp_path):
    index, embedder = small_index
    llm = FakeLLM([answer([("병가는 연 0일입니다.", ["dec36728:a5"])])] * 2)
    service = make_service(index, embedder, llm, tmp_path)
    result = service.answer("병가는 며칠까지 쓸 수 있나요?", ConversationState())
    assert result.answer.status == "escalation_required"  # "0일" is not found inside "60일"
    state = ConversationState()
    state.add("재직 12년인데 연가는 며칠인가요?")  # turn 1
    state.add("질문 하나")
    state.add("재직 12년인데 반일 연가는요?")  # turn 3: said again, so it now counts from turn 3
    for number in range(4):
        state.add(f"질문 {number}")  # turns 4-7: turn 1 is out of the last five, turn 3 is not
    assert [(c.text, c.turn) for c in state.conditions] == [("재직 12년인데", 3)]


def test_a_reply_that_is_not_answered_shows_no_headline_or_points(small_index, tmp_path):
    index, embedder = small_index
    llm = FakeLLM(
        [
            {
                "status": "needs_clarification",
                "one_line": "병가는 100일입니다.",
                "points": [{"text": "병가는 100일입니다.", "evidence_ids": ["dec36728:a5"]}],
                "clarification_question": "어떤 병가인지 알려 주세요.",
                "missing_conditions": ["병가 종류"],
            }
        ]
    )
    result = make_service(index, embedder, llm, tmp_path).answer("병가는 며칠인가요?", ConversationState())
    assert result.answer.status == "needs_clarification"
    assert result.answer.one_line is None and result.answer.points == [] and result.answer.conditions == []


def test_instructions_inside_documents_are_data(index_dir, tmp_path):
    index, embedder = make_index(index_dir, texts=(LAW, RULE, INJECTION))
    llm = FakeLLM(
        tool_calls=[
            ("search_regulations", {"query": "근무복"}),
            ("submit_answer", answer([("근무복은 자율입니다.", ["ins1:a1"])])),
        ]
    )
    service = make_service(index, embedder, llm, tmp_path)
    result = service.answer_with_tools("근무복 규정이 있나요?", ConversationState())
    assert set(llm.offered) == {
        "search_regulations",
        "get_article",
        "get_chunk",
        "submit_answer",
        "request_clarification",
        "escalate",
    }
    assert [c["name"] for c in result.tool_calls] == ["search_regulations", "submit_answer"]
    assert result.answer.status == "answered" and result.answer.retrieval_mode == "tool"
    assert not (tmp_path / "slack").exists()


def test_tool_mode_only_read_only_tools_and_step_limit(small_index, tmp_path):
    assert all(not spec.side_effect for spec in regulation_tools())
    names = {spec.name for spec in regulation_tools()}
    assert not names & {"create_calendar_event", "post_slack_message", "write_file", "run_sql"}
    index, embedder = small_index
    llm = FakeLLM(tool_calls=[("search_regulations", {"query": "병가"})] * 6)
    service = make_service(index, embedder, llm, tmp_path)
    result = service.answer_with_tools("병가는 며칠?", ConversationState())
    assert result.answer.status == "escalation_required" and "최대 단계" in result.answer.escalation_reason
    lines = (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["status"] == "pending"


def test_model_unavailable_gives_source_cards(small_index, tmp_path):
    index, embedder = small_index
    service = make_service(index, embedder, FakeLLM([]), tmp_path)
    result = service.answer("병가는 1년에 며칠인가요?", ConversationState())
    assert result.answer.status == "escalation_required" and result.answer.fallback_used and result.cards
