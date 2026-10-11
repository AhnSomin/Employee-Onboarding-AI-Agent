"""규정 Q&A: answers from the regulation index with the source text beside every answer.

The page only reads the index (built by scripts/build_regulation_index.py) and
never calls Calendar, Slack or mail. Without a usable index it still takes
questions and answers source_unavailable with the command that fixes it.
"""

from __future__ import annotations

import json

import streamlit as st

from onboarding_agent.config import get_settings
from onboarding_agent.rag import service as rag_service
from onboarding_agent.rag.answer import DISCLAIMER, NOT_CHECKED, AnswerResult, ConversationState
from onboarding_agent.rag.answer import _dot as dot
from onboarding_agent.rag.escalation import PENDING_NOTICE
from onboarding_agent.rag.index import IndexUnavailable
from onboarding_agent.rag.models import RegAnswer
from onboarding_agent.rag.parse_law import IMAGE_MARKER
from onboarding_agent.rag.validate import CHECK_NAMES

K = "reg_"
ss = st.session_state
settings = get_settings()
EXAMPLES = (
    "점심시간은 몇 시부터 몇 시까지인가요?",
    "병가를 6일 넘게 쓰면 진단서가 필요한가요?",
    "당직 신고는 근무 시작 몇 분 전에 해야 하나요?",
)
STATUS = {
    "answered": ("답변", "green"),
    "needs_clarification": ("확인 질문", "blue"),
    "escalation_required": ("담당자 확인 필요", "orange"),
    "source_unavailable": ("자료 없음", "gray"),
}
CHECK_LABELS = {"passed": "확인됨", "failed": "실패", "warning": "주의", "not_applicable": "해당 없음"}
BUILD_COMMAND = "uv run python scripts/build_regulation_index.py build --source <규정 원문 폴더>"


@st.cache_resource(show_spinner="규정 색인을 불러오는 중…")
def load_runtime(key: str):
    """Cached per index version and embedding settings (key); the page never builds the index."""
    return rag_service.build_runtime(get_settings())


def runtime_key() -> str:
    current = settings.reg_index_dir / "CURRENT"
    version = current.read_text(encoding="utf-8").strip() if current.is_file() else "none"
    return f"{settings.reg_index_dir}|{version}|{settings.gemini_embed_model}|{settings.gemini_embed_dim}"


def get_runtime():
    try:
        return load_runtime(runtime_key()), None
    except IndexUnavailable as exc:
        return None, str(exc)


def state() -> ConversationState:
    if K + "state" not in ss:
        ss[K + "state"] = ConversationState()
    return ss[K + "state"]


def ask(question: str) -> None:
    question = question.strip()
    if not question:
        return
    runtime, problem = get_runtime()
    if runtime is None:
        ss[K + "result"] = None
        ss[K + "unavailable"] = (question, problem)
        return
    ss[K + "unavailable"] = None
    if runtime.embedder is not None:
        runtime.embedder.query_purpose = "query:app"
    tool_mode = ss.get(K + "tool_mode", False)
    with st.spinner("규정을 찾고 답을 쓰는 중…"):
        if tool_mode:
            result = runtime.service.answer_with_tools(question, state(), purpose="tool_mode")
        else:
            result = runtime.service.answer(question, state(), purpose="app_question")
    ss[K + "result"] = (question, result)


def on_submit() -> None:
    ask(ss.get(K + "question", ""))


def on_example(question: str) -> None:
    ss[K + "question"] = question
    ask(question)


def clear_conditions() -> None:
    state().clear()


def render_answer(question: str, result: AnswerResult, runtime) -> None:
    answer: RegAnswer = result.answer
    label, color = STATUS[answer.status]
    st.markdown(f"**질문** {question}")
    with st.container(border=True):
        st.badge(label, color=color)
        if answer.degraded_retrieval:
            st.badge("키워드 검색만 사용", color="orange")
        if answer.one_line:
            st.markdown(f"### {answer.one_line}")
        for point in answer.points:
            st.markdown(f"- {point.text}  `{', '.join(point.evidence_ids)}`")
        if answer.conditions:
            st.markdown("**조건·예외**")
            for condition in answer.conditions:
                st.markdown(f"- {condition.text}  `{', '.join(condition.evidence_ids)}`")
        if answer.status == "needs_clarification":
            st.info(answer.clarification_question or "조건을 조금 더 알려 주세요.")
            if answer.missing_conditions:
                st.caption("필요한 조건: " + ", ".join(answer.missing_conditions))
        if answer.status == "escalation_required":
            st.warning(PENDING_NOTICE + (f"\n\n사유: {answer.escalation_reason}" if answer.escalation_reason else ""))
        if answer.status == "source_unavailable":
            st.warning(answer.escalation_reason or "색인에 이 질문에 맞는 자료가 없습니다.")
        if answer.checks.get("단서·예외") == "warning":
            st.warning("원문의 단서·예외 확인 필요")
        st.caption(f"기준: {answer.basis_version or '미확인'}")
        st.caption(DISCLAIMER)

    render_cards(result, runtime)
    executed = {
        name: answer.checks[name] for name in CHECK_NAMES if answer.checks.get(name) not in (None, "not_applicable")
    }
    checks = " · ".join(f"{name} {CHECK_LABELS[value]}" for name, value in executed.items()) or "실행된 검사 없음"
    st.caption(
        f"검색 방식: {answer.retrieval_mode}{' (임베딩 장애로 키워드만)' if answer.degraded_retrieval else ''} · "
        f"생성 모델: {answer.model_used or '호출 안 함'}"
        f"{' (대체 모델)' if answer.fallback_used and answer.model_used else ''}"
        f" · 색인 버전: {runtime.index.version} · 검사: {checks} · 처리 {result.seconds}초"
    )
    st.caption(NOT_CHECKED)
    if result.tool_calls:
        with st.expander(f"도구 호출 {len(result.tool_calls)}회"):
            st.code(json.dumps(result.tool_calls, ensure_ascii=False, indent=1), language="json")


def render_cards(result: AnswerResult, runtime) -> None:
    if not result.cards:
        return
    cited = set(result.cited_ids)
    st.subheader("근거" if cited else "관련 원문")
    for hit in result.cards[:8]:
        chunk = hit.chunk
        doc = runtime.index.docs.get(chunk.doc_id)
        when = f"시행 {dot(doc.effective_date)}" if doc and doc.effective_date else "시행일 미확인"
        kind = "가상 자료" if doc and doc.is_fictional else "실제 법령"
        tags = ["인용" if chunk.chunk_id in cited else ("참조 조문" if hit.via == "ref" else "검색됨"), kind]
        if chunk.kind == "table":
            reviewed = doc is not None and "manual_transcription" not in doc.unverified
            tags.append("표 전사본(검수 완료)" if reviewed else "표 전사본(검수 대기)")
        elif chunk.kind == "annex":
            tags.append("별표")
        title = f"{doc.doc_title if doc else chunk.doc_id} {runtime.service.label(chunk)}"
        with st.container(border=True):
            st.markdown(f"**{title}**  `{chunk.chunk_id}`")
            if chunk.kind == "table":
                st.badge(tags[-1], color="orange" if "대기" in tags[-1] else "green")
            st.caption(
                " · ".join(tags)
                + f" · {when}"
                + (f" · {doc.version_label}" if doc and doc.version_label else "")
                + f" · {location(chunk.location)}"
            )
            if chunk.section_path:
                st.caption(chunk.section_path)
            with st.expander("원문 보기", expanded=chunk.chunk_id in cited):
                st.text(chunk.text)
                if IMAGE_MARKER in chunk.text:
                    tables = [r for r in chunk.refs if (t := runtime.index.chunk(r)) and t.kind == "table"]
                    st.caption(
                        "이 조문의 표는 원문 PDF에 이미지로 들어 있습니다. 옮겨 적은 표 전사본: " + ", ".join(tables)
                        if tables
                        else "이 조문의 표는 원문 PDF에 이미지로 들어 있어 색인하지 않았습니다(OCR하지 않음)."
                    )
                if chunk.kind == "table":
                    st.caption("원문 PDF의 이미지 표를 옮겨 적은 것입니다. 원문과 다를 수 있으니 PDF로 확인하세요.")


def location(loc: dict) -> str:
    if "pdf_page" in loc:
        printed = loc.get("printed_page")
        pages = f"PDF {loc['pdf_page']}쪽" + (
            f"~{loc['pdf_page_end']}쪽" if loc.get("pdf_page_end") != loc["pdf_page"] else ""
        )
        return pages + (f" (인쇄 {printed}쪽)" if printed else " (인쇄 쪽수 미확인)")
    if loc.get("lines"):
        return f"{loc['lines'][0]}~{loc['lines'][1]}행"
    return "위치 미확인"


def render_admin(runtime, problem: str | None) -> None:
    st.subheader("색인 상태")
    if runtime is None:
        st.error(f"색인을 쓸 수 없습니다: {problem}")
        st.code(BUILD_COMMAND, language="bash")
        return
    manifest = runtime.index.manifest
    st.success(f"색인 {runtime.index.version} · 청크 {len(runtime.index.chunks)}개 · 문서 {len(runtime.index.docs)}개")
    embedding = manifest["embedding"]
    st.caption(
        f"임베딩: {embedding['model']} · {embedding['dim']}차원 · 문서 {embedding['doc_task']} · "
        f"질문 {embedding['query_task']} · {embedding['normalization']} · {manifest['search']}"
    )
    rows = [
        {
            "문서": d["title"],
            "버전": d["version_label"],
            "시행일": d["effective_date"] or "미확인",
            "청크": d["chunks"],
            "미확인 항목": ", ".join(d["unverified"]),
        }
        for d in manifest["documents"]
    ]
    st.dataframe(rows, hide_index=True)
    with st.expander(f"제외 항목 {len(manifest['excluded'])}개"):
        st.dataframe(
            [{"문서": e.get("doc", ""), "종류": e["kind"], "내용": e["detail"]} for e in manifest["excluded"]],
            hide_index=True,
        )
    if manifest.get("unindexed"):
        st.warning("미색인 문서: " + ", ".join(u["title"] for u in manifest["unindexed"]))
    st.markdown("**구축·갱신 명령** (화면에서는 실행하지 않습니다)")
    st.code(BUILD_COMMAND + "\nuv run python scripts/build_regulation_index.py status", language="bash")
    usage = runtime.meter.totals()
    st.caption("누적 호출(규정 Q&A): " + json.dumps(usage, ensure_ascii=False))


# --- page --------------------------------------------------------------------------------

st.title("규정 Q&A")
st.caption("국가공무원 복무 규정 원문에서 근거를 찾아 쉬운 말로 답합니다. " + DISCLAIMER)
runtime, problem = get_runtime()
ask_tab, admin_tab = st.tabs(["질문", "자료 관리"])

with ask_tab:
    conditions = state().conditions
    with st.container(horizontal=True):
        st.caption("적용 중인 조건: " + (state().describe() if conditions else "(없음)"))
        st.button("조건 지우기", key=K + "clear", on_click=clear_conditions, disabled=not conditions)
    st.text_input("질문", key=K + "question", placeholder="예: 연가는 반일 단위로 쓸 수 있나요?")
    with st.container(horizontal=True):
        st.button("질문하기", type="primary", key=K + "ask", on_click=on_submit)
        st.toggle("도구 모드(함수 호출)", key=K + "tool_mode")
    with st.container(horizontal=True):
        for number, example in enumerate(EXAMPLES):
            st.button(example, key=f"{K}example{number}", on_click=on_example, args=(example,))

    unavailable = ss.get(K + "unavailable")
    result = ss.get(K + "result")
    if unavailable:
        question, why = unavailable
        st.markdown(f"**질문** {question}")
        with st.container(border=True):
            st.badge(STATUS["source_unavailable"][0], color="gray")
            st.warning(f"규정 색인을 쓸 수 없어 답할 수 없습니다: {why}")
            st.code(BUILD_COMMAND, language="bash")
    elif result and runtime is not None:
        render_answer(result[0], result[1], runtime)
    elif runtime is None:
        st.info(f"규정 색인이 아직 없습니다: {problem}")

with admin_tab:
    render_admin(runtime, problem)
