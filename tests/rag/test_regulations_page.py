"""Headless runs of the 규정 Q&A page (AppTest) with a test index and a scripted model."""

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from onboarding_agent.config import REPO_ROOT, get_settings
from onboarding_agent.rag import service as rag_service
from onboarding_agent.rag.escalation import PENDING_NOTICE
from onboarding_agent.rag.usage import UsageMeter

from .conftest import FakeLLM, make_index, make_service

PAGE = REPO_ROOT / "app" / "views" / "regulations.py"
TIMEOUT = 30


def page_text(app: AppTest) -> str:
    parts = [m.value for m in app.markdown] + [c.value for c in app.caption]
    parts += [w.value for w in app.warning] + [i.value for i in app.info] + [e.value for e in app.error]
    parts += [s.value for s in app.success]
    parts += [b.proto.label for b in app.get("badge")]
    return "\n".join(str(p) for p in parts)


@pytest.fixture(autouse=True)
def fresh_cache():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    """The page's runtime is the small test index with a scripted model."""

    def install(answers):
        index_dir = get_settings().reg_index_dir
        index, embedder = make_index(index_dir)
        llm = FakeLLM(answers)
        service = make_service(index, embedder, llm, tmp_path)
        runtime = rag_service.Runtime(
            get_settings(), UsageMeter(tmp_path / "usage.jsonl"), None, embedder, index, service.retriever, service
        )
        monkeypatch.setattr(rag_service, "build_runtime", lambda settings, **_: runtime)
        return llm

    return install


def ask(app: AppTest, question: str) -> AppTest:
    app.text_input(key="reg_question").set_value(question)
    app.button(key="reg_ask").click()
    return app.run()


def test_without_index_questions_get_source_unavailable_and_the_build_command():
    app = AppTest.from_file(str(PAGE), default_timeout=TIMEOUT).run()
    assert "규정 색인이 아직 없습니다" in page_text(app)
    app = ask(app, "연가는 며칠인가요?")
    assert "자료 없음" in page_text(app)
    assert any("build_regulation_index.py build" in c.value for c in app.code)


def test_answer_card_evidence_card_and_checks(scripted):
    llm = scripted(
        [
            {
                "status": "answered",
                "one_line": "낮 12시부터 오후 1시까지입니다.",
                "points": [{"text": "점심시간은 낮 12시부터 오후 1시까지입니다.", "evidence_ids": ["dec36728:a4"]}],
                "conditions": [
                    {"text": "다만, 1시간의 범위에서 달리 정할 수 있습니다.", "evidence_ids": ["dec36728:a4"]}
                ],
            }
        ]
    )
    app = AppTest.from_file(str(PAGE), default_timeout=TIMEOUT).run()
    app = ask(app, "점심시간은 몇 시부터인가요?")
    text = page_text(app)
    assert not app.exception
    assert "답변" in text and "낮 12시부터 오후 1시까지입니다." in text
    assert "국가공무원 복무규정 제4조(근무시간 등)" in text and "인용" in text and "실제 법령" in text
    assert "시행 2026. 10. 2." in text and "근거 ID 확인됨" in text
    assert "안내용 답변이며, 개인별 적용은 인사담당자에게 확인하세요." in text
    assert "의미 해석이 맞는지는 자동으로 검증하지 않습니다" in text
    assert len(llm.prompts) == 1


def test_escalation_and_clarification_cards(scripted):
    scripted(
        [
            {
                "status": "needs_clarification",
                "clarification_question": "임신 중이신가요?",
                "missing_conditions": ["임신 여부"],
            },
            {"status": "escalation_required", "escalation_reason": "근거 부족"},
        ]
    )
    app = AppTest.from_file(str(PAGE), default_timeout=TIMEOUT).run()
    app = ask(app, "저도 병가를 쓸 수 있나요?")
    assert "확인 질문" in page_text(app) and "임신 중이신가요?" in page_text(app)
    app = ask(app, "병가 승인은 누가 하나요?")
    assert PENDING_NOTICE in page_text(app) and "담당자 확인 필요" in page_text(app)


def test_admin_tab_shows_index_without_building(scripted):
    scripted([])
    app = AppTest.from_file(str(PAGE), default_timeout=TIMEOUT).run()
    text = page_text(app)
    assert "청크" in text and "화면에서는 실행하지 않습니다" in text
    assert any("build_regulation_index.py status" in c.value for c in app.code)
