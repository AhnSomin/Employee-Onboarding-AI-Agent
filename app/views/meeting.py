"""회의록 → 액션 page: read, extract, review, approve and execute (spec 8.4).

Business rules live in onboarding_agent (review, executor); this page wires
widgets to them. State that must survive reruns, page changes and refreshes
sits outside widgets: non-widget session keys under "meeting." and the
approved meeting id in the URL (?meeting=...). Approval and execution run in
button callbacks, which a second click cannot interrupt halfway.
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from onboarding_agent.actions.executor import DRY_RUN_PREFIX, ExecutionReport, execute_meeting, is_done
from onboarding_agent.config import REPO_ROOT, ConfigError, get_settings
from onboarding_agent.meeting import review
from onboarding_agent.meeting.dates import format_due
from onboarding_agent.meeting.extract import extract_meeting
from onboarding_agent.meeting.loader import LoadedMeeting, MeetingInputError, load_meeting
from onboarding_agent.meeting.models import ExtractionResult
from onboarding_agent.meeting.render import calendar_event, slack_summary
from onboarding_agent.meeting.roster import Roster, RosterError, load_roster
from onboarding_agent.store import StoreUnavailable, get_store

K = "meeting."
SAMPLES_DIR = REPO_ROOT / "data" / "samples"
# Non-widget keys holding the current work; cleared when another meeting is loaded.
WORK_KEYS = (
    "loaded", "title", "date", "force", "result", "rows", "summary", "decisions", "exclude",
    "editor_version", "progress_log", "approve_error", "input_error", "current_id", "report",
)
PATH_LABELS = {
    "function_calling": "Function calling",
    "structured": "구조화 출력(대체)",
    "rule_based": "규칙 기반",
}
CALENDAR_LABELS = {"created": "생성", "skipped_existing": "건너뜀(이미 있음)", "failed": "실패", "dry_run": "DRY_RUN"}
SLACK_LABELS = {"sent": "발송", "skipped_existing": "건너뜀(이미 보냄)", "failed": "실패", "dry_run": "DRY_RUN"}

ss = st.session_state


# --- setup -------------------------------------------------------------------


def _roster(settings) -> Roster:
    try:
        return load_roster(settings.roster_path)
    except RosterError as exc:
        st.warning(str(exc))
        return Roster()


try:
    settings = get_settings()
    store = get_store(settings)
except (ConfigError, StoreUnavailable) as exc:
    st.title("회의록 → 액션")
    st.error(str(exc))
    st.stop()
roster = _roster(settings)


def _clear_work() -> None:
    for name in WORK_KEYS:
        ss.pop(K + name, None)


def _start_over() -> None:
    _clear_work()
    for widget in ("upload", "pasted", "sample"):
        ss.pop(K + widget, None)
    if "meeting" in st.query_params:
        del st.query_params["meeting"]


def _copy_widget(name: str) -> None:
    """Keep a widget value in a non-widget key so it survives page changes."""
    ss[K + name] = ss[K + name + "_input"]


# --- loading -------------------------------------------------------------------


def _load(**kwargs) -> None:
    try:
        loaded = load_meeting(**kwargs)
    except MeetingInputError as exc:
        ss[K + "input_error"] = str(exc)
        return
    _clear_work()
    ss[K + "loaded"] = loaded
    ss[K + "title"] = loaded.title_suggestion
    ss[K + "date"] = loaded.meeting_date
    ss[K + "force"] = get_settings().force_fallback


def _on_upload() -> None:
    file = ss.get(K + "upload")
    if file is not None:
        _load(data=file.getvalue(), filename=file.name)


def _on_sample() -> None:
    name = ss.get(K + "sample")
    if name:
        _load(data=(SAMPLES_DIR / name).read_bytes(), filename=name)


def _on_paste() -> None:
    _load(text=ss.get(K + "pasted", ""))


def _render_source_picker() -> None:
    st.subheader("1. 회의록 불러오기")
    upload_tab, paste_tab, sample_tab = st.tabs(["파일 올리기", "붙여넣기", "데모 샘플"])
    with upload_tab:
        st.file_uploader("회의록 파일 (.txt, .md)", type=["txt", "md"], key=K + "upload", on_change=_on_upload)
    with paste_tab:
        st.text_area("회의록 내용", key=K + "pasted", height=220)
        st.button("붙여넣은 내용 읽기", key=K + "read_pasted", on_click=_on_paste)
    with sample_tab:
        st.selectbox(
            "가상 회의록 샘플",
            [p.name for p in sorted(SAMPLES_DIR.glob("*.txt"))],
            index=None,
            placeholder="샘플을 고르세요",
            key=K + "sample",
            on_change=_on_sample,
        )
    if error := ss.pop(K + "input_error", None):
        st.error(error)


# --- extraction ------------------------------------------------------------------


def _run_extraction(loaded: LoadedMeeting) -> None:
    log: list[str] = []
    started = time.perf_counter()
    with st.status("회의록을 분석하고 있습니다…", expanded=True) as status:

        def write(line: str, slot=None) -> None:
            (slot or st).write(line)

        first = f"① 파일 읽기 — {len(loaded.text):,}자" + (f" ({loaded.encoding})" if loaded.encoding else "")
        write(first)
        model_slot = st.empty()

        def progress(stage: str, detail: str) -> None:
            if stage == "llm":
                write(f"② 모델 추출 — {detail} 호출 중…", model_slot)
            elif stage == "structured":
                write(f"↳ 도구 호출 결과가 불완전해 구조화 출력으로 다시 추출합니다 ({detail})")
            elif stage == "rules":
                write(f"② 모델 추출 — 규칙 기반으로 추출합니다 ({detail})", model_slot)
            elif stage == "validate":
                write("③ 검증 — 근거 인용·담당자·기한을 확인하는 중…")

        result = extract_meeting(
            loaded.text,
            title=ss[K + "title"],
            meeting_date=ss[K + "date"],
            source_filename=loaded.source_filename,
            roster=roster,
            force_fallback=ss[K + "force"],
            progress=progress,
        )
        elapsed = time.perf_counter() - started
        second = f"② 모델 추출 — {result.model_used or 'LLM 없음'} · {PATH_LABELS[result.extraction_path]}"
        write(second, model_slot)
        items = result.action_items
        third = (
            f"③ 검증 — 항목 {len(items)}개 · 미확정 "
            f"{sum(1 for i in items if 'unconfirmed' in (i.owner_status, i.due_status))}개 · "
            f"검토 필요 {sum(1 for i in items if i.needs_review)}개 · 경고 {len(result.warnings)}개"
        )
        write(third)
        log = [first, second, third]
        status.update(label=f"분석 완료 · {elapsed:.1f}초", state="complete", expanded=False)

    ss[K + "result"] = result
    ss[K + "rows"] = review.rows_from_result(result)
    ss[K + "summary"] = "\n".join(result.meeting.summary)
    ss[K + "decisions"] = "\n".join(d.text for d in result.meeting.decisions)
    ss[K + "editor_version"] = ss.get(K + "editor_version", 0) + 1
    ss[K + "progress_log"] = (log, elapsed)


def _render_loaded(loaded: LoadedMeeting) -> None:
    locked = K + "result" in ss
    st.subheader("1. 회의록 확인")
    with st.container(border=True):
        st.markdown(
            f"**{loaded.source_filename or '붙여넣은 회의록'}** · {len(loaded.text):,}자"
            + (f" · {loaded.encoding}" if loaded.encoding else "")
        )
        with st.expander("원문 보기"):
            st.text(loaded.text)
        left, right = st.columns([3, 1])
        left.text_input(
            "회의 제목", value=ss[K + "title"], key=K + "title_input",
            on_change=_copy_widget, args=("title",), disabled=locked,
        )
        right.date_input(
            "회의 날짜 (기한 계산 기준)", value=ss[K + "date"], key=K + "date_input",
            on_change=_copy_widget, args=("date",), disabled=locked,
        )
        if not loaded.meeting_date_detected:
            st.warning(
                "본문에서 회의 날짜를 찾지 못해 오늘 날짜를 넣었습니다. "
                "'내일', '다음 주 금요일' 같은 기한의 기준이니 확인해 주세요."
            )
        st.toggle(
            "LLM 없이 규칙 기반으로 추출 (시연용 강제 폴백)", value=ss[K + "force"],
            key=K + "force_input", on_change=_copy_widget, args=("force",), disabled=locked,
        )
        if locked:
            st.caption("회의 날짜는 기한 계산의 기준이라 추출 뒤에는 바꿀 수 없습니다. 바꾸려면 '다시 추출'을 누르세요.")
        buttons = st.container(horizontal=True)
        if not locked:
            if buttons.button("추출", type="primary", key=K + "extract"):
                _run_extraction(loaded)
                st.rerun()  # lock the inputs and show the review from saved state
        else:
            buttons.button("다시 추출", key=K + "reextract", on_click=_reset_extraction)
        buttons.button("다른 회의록", key=K + "other", on_click=_start_over)


def _reset_extraction() -> None:
    for name in ("result", "rows", "summary", "decisions", "progress_log", "approve_error"):
        ss.pop(K + name, None)


# --- review --------------------------------------------------------------------


def _render_progress_log() -> None:
    log = ss.get(K + "progress_log")
    if log:
        lines, elapsed = log
        with st.status(f"분석 완료 · {elapsed:.1f}초", state="complete", expanded=False):
            for line in lines:
                st.write(line)


def _render_tool_panel(result: ExtractionResult) -> None:
    tools = result.tool_calls
    with st.container(border=True):
        st.markdown("**에이전트가 호출한 도구**")
        path, model, turns = st.columns(3)
        path.metric("추출 경로", PATH_LABELS[result.extraction_path])
        model.metric("사용 모델", result.model_used or "없음")
        turns.metric("턴 수", tools.turns if tools else 0)
        if tools and tools.calls:
            st.markdown(" · ".join(f"`{name}` ×{count}" for name, count in tools.calls.items()))
            extra = []
            if tools.rejected:
                extra.append(f"인자 오류로 버린 호출 {tools.rejected}건")
            if tools.merged_duplicates:
                extra.append(f"합친 중복 제안 {tools.merged_duplicates}건")
            if not tools.finished:
                extra.append("finish_extraction 없음")
            if extra:
                st.caption(" · ".join(extra))
        else:
            st.caption("LLM 도구 호출 없이 규칙 기반으로 추출했습니다.")
        if result.injection_sentences:
            st.caption(
                f"회의록 속 AI 대상 지시문 {len(result.injection_sentences)}건은 데이터로만 다뤘습니다"
                + (f" (그 문장에서 나온 항목 {result.injection_blocked}건 제외)" if result.injection_blocked else "")
                + "."
            )


def _merge_edits(editor_key: str) -> None:
    state = ss.get(editor_key) or {}
    ss[K + "rows"] = review.apply_edits(
        ss[K + "rows"],
        state.get("edited_rows", {}),
        state.get("added_rows", []),
        state.get("deleted_rows", []),
    )
    ss[K + "editor_version"] = ss.get(K + "editor_version", 0) + 1  # fresh editor from merged rows


def _render_table() -> None:
    rows = ss[K + "rows"]
    columns = ["include", "task", "owner", "owner_ok", "due_date", "due_time", "due_ok",
               "due_text", "status", "evidence", "notes", "item_id", "needs_review"]
    frame = pd.DataFrame(rows, columns=columns)
    editor_key = f"{K}editor.{ss.get(K + 'editor_version', 0)}"
    st.data_editor(
        frame,
        key=editor_key,
        on_change=_merge_edits,
        args=(editor_key,),
        num_rows="dynamic",
        hide_index=True,
        column_order=columns[:11],
        column_config={
            "include": st.column_config.CheckboxColumn("포함", width="small"),
            "task": st.column_config.TextColumn("할 일", width="large", required=True),
            "owner": st.column_config.SelectboxColumn(
                "담당자", options=review.owner_options(roster, rows), required=True
            ),
            "owner_ok": st.column_config.CheckboxColumn("담당 확정", help="담당자를 고르면 확정됩니다."),
            "due_date": st.column_config.DateColumn("기한", format="YYYY-MM-DD"),
            "due_time": st.column_config.TimeColumn("시간", format="HH:mm", step=900),
            "due_ok": st.column_config.CheckboxColumn("기한 확정", help="날짜를 고르면 확정됩니다."),
            "due_text": st.column_config.TextColumn("원문 기한", disabled=True),
            "status": st.column_config.TextColumn("상태", disabled=True),
            "evidence": st.column_config.TextColumn("근거 인용", disabled=True, width="large"),
            "notes": st.column_config.TextColumn("검증 노트", disabled=True, width="large"),
        },
    )
    weekend = sum(1 for r in rows if "주말" in r["status"])
    if weekend:
        st.caption(f"⚠ 주말 기한 {weekend}건이 있습니다. 날짜가 맞는지 확인해 주세요.")


def _current_approval(result: ExtractionResult) -> review.Approval:
    return review.build_approval(
        result,
        ss[K + "rows"],
        summary=ss[K + "summary"].splitlines(),
        decisions=ss[K + "decisions"].splitlines(),
        exclude_unconfirmed=ss.get(K + "exclude", False),
        roster=roster,
        now=datetime.now(settings.tz),
    )


def _render_preview(approval: review.Approval) -> None:
    items = approval.approved_items
    with st.expander(f"미리보기 — 캘린더 일정 {len(items)}개와 Slack 메시지 1건", expanded=False):
        st.caption("승인하면 아래 내용 그대로 실행됩니다." + (" (지금은 DRY_RUN이라 실제로 만들거나 보내지 않습니다.)" if settings.actions_dry_run else ""))
        for item in items:
            event = calendar_event(item, approval.meeting, settings.timezone)
            when = format_due(item.due_date, item.due_time) + (" 종일" if item.due_time is None else " (1시간)")
            st.markdown(f"- 📅 **{event['summary']}** — {when}")
        st.code(slack_summary(approval.meeting, items).text, language=None)


def _approve_and_execute() -> None:
    if ss.get(K + "current_id"):
        return  # a queued second click after approval
    result: ExtractionResult = ss[K + "result"]
    current = get_settings()
    current_store = get_store(current)
    approval = review.approve_meeting(
        current_store,
        result,
        ss[K + "rows"],
        summary=ss[K + "summary"].splitlines(),
        decisions=ss[K + "decisions"].splitlines(),
        exclude_unconfirmed=ss.get(K + "exclude", False),
        roster=_roster(current),
        now=datetime.now(current.tz),
    )
    if approval.meeting is None:
        ss[K + "approve_error"] = approval.plan.problems
        return
    meeting_id = approval.meeting.meeting_id
    ss[K + "current_id"] = meeting_id
    st.query_params["meeting"] = meeting_id
    ss[K + "report"] = execute_meeting(meeting_id, store=current_store, settings=current)


def _open_existing(meeting_id: str) -> None:
    ss[K + "current_id"] = meeting_id
    st.query_params["meeting"] = meeting_id


def _render_review(result: ExtractionResult) -> None:
    _render_progress_log()
    _render_tool_panel(result)
    if result.warnings:
        with st.expander(f"검증 경고 {len(result.warnings)}건", expanded=True):
            for warning in result.warnings:
                st.warning(warning)

    st.subheader("2. 요약과 결정사항")
    left, right = st.columns(2)
    left.text_area("요약 (한 줄에 하나)", value=ss[K + "summary"], key=K + "summary_input",
                   on_change=_copy_widget, args=("summary",), height=160)
    right.text_area("결정사항 (한 줄에 하나)", value=ss[K + "decisions"], key=K + "decisions_input",
                    on_change=_copy_widget, args=("decisions",), height=160)

    st.subheader("3. 액션 아이템")
    st.caption("담당자를 고르거나 기한을 입력하면 확정으로 바뀝니다. 미확정 항목이 포함되어 있으면 승인할 수 없습니다.")
    _render_table()

    st.subheader("4. 승인과 실행")
    st.checkbox("미확정 항목은 제외하고 승인 (제외한 항목은 초안으로 저장)", value=ss.get(K + "exclude", False),
                key=K + "exclude_input", on_change=_copy_widget, args=("exclude",))
    approval = _current_approval(result)
    if approval.plan.problems:
        st.warning("승인 전에 확인해 주세요.\n" + "\n".join(f"- {p}" for p in approval.plan.problems))
    else:
        excluded = f" · 제외 {len(approval.plan.exclude)}개(초안 저장)" if approval.plan.exclude else ""
        st.info(f"승인 대상 {len(approval.approved_items)}개{excluded}")
        _render_preview(approval)

    previous = [
        m for m in store.list_meetings()
        if m.source_hash == result.meeting.source_hash and m.meeting_id != result.meeting.meeting_id
    ]
    if previous:
        st.warning(
            f"같은 회의록이 이미 승인된 적이 있습니다('{previous[-1].title}'). "
            "다시 승인하면 일정과 메시지가 한 번 더 만들어집니다."
        )
        st.button("기존 결과 보기", key=K + "open_previous", on_click=_open_existing, args=(previous[-1].meeting_id,))
    if problems := ss.pop(K + "approve_error", None):
        st.error("승인하지 못했습니다.\n" + "\n".join(f"- {p}" for p in problems))
    st.button("승인하고 실행", type="primary", key=K + "approve", disabled=bool(approval.plan.problems),
              on_click=_approve_and_execute)


# --- approved meeting ------------------------------------------------------------


def _retry(meeting_id: str) -> None:
    current = get_settings()
    ss[K + "report"] = execute_meeting(meeting_id, store=get_store(current), settings=current)


def _stored_state(value: str | None) -> str:
    if not value:
        return "미실행"
    return "DRY_RUN 기록" if value.startswith(DRY_RUN_PREFIX) else "완료"


def _render_report_payloads(report: ExecutionReport) -> None:
    if not (report.calendar_payloads or report.slack_message):
        return
    title = "보낸 내용" if not report.dry_run else "DRY_RUN 페이로드 (실제로 만들거나 보내지 않음)"
    with st.expander(title):
        for payload in report.calendar_payloads:
            start = payload["start"].get("date") or payload["start"].get("dateTime")
            st.markdown(f"- 📅 `{payload['id']}` **{payload['summary']}** — {start}")
        if report.slack_message:
            st.caption("Slack 스레드 답글" if report.slack_thread_reply else "Slack 요약 메시지")
            st.code(report.slack_message.text, language=None)


def _render_approved(meeting_id: str) -> None:
    meeting = store.get_meeting(meeting_id)
    if meeting is None:
        st.warning("저장된 회의를 찾지 못했습니다.")
        st.button("새 회의록 처리", key=K + "new_missing", on_click=_start_over)
        return
    items = store.list_items(meeting_id=meeting_id)
    targets = [i for i in items if i.status in ("approved", "done")]
    drafts = [i for i in items if i.status == "draft"]
    st.success(
        f"'{meeting.title}' ({format_due(meeting.meeting_date)}) 승인 완료 · "
        f"실행 대상 {len(targets)}개 · 초안으로 남긴 항목 {len(drafts)}개"
    )
    report: ExecutionReport | None = ss.get(K + "report")
    if report is not None and report.meeting_id != meeting_id:
        report = None
    if report:
        for warning in report.warnings:
            st.warning(warning)
    by_id = {r.item_id: r for r in report.results} if report else {}

    table = []
    for item in targets:
        result = by_id.get(item.item_id)
        table.append(
            {
                "할 일": item.task,
                "담당자": item.owner_name if item.owner_slack_id else f"{item.owner_name}(Slack 미등록)",
                "기한": format_due(item.due_date, item.due_time) if item.due_date else "",
                "Calendar": CALENDAR_LABELS[result.calendar] if result else _stored_state(item.calendar_event_id),
                "Slack": SLACK_LABELS[result.slack] if result else _stored_state(item.slack_ts),
                "오류": (result.error or "") if result else "",
            }
        )
    st.dataframe(pd.DataFrame(table), hide_index=True)
    mode = "DRY_RUN" if (report.dry_run if report else settings.actions_dry_run) else "실제 실행"
    st.caption(f"실행 모드: {mode} · 같은 회의를 다시 실행해도 이미 처리된 일정과 메시지는 건너뜁니다.")
    if report:
        _render_report_payloads(report)

    dry_run = report.dry_run if report else settings.actions_dry_run
    unfinished = [
        i for i in targets
        if not (is_done(i.calendar_event_id, dry_run) and is_done(i.slack_ts, dry_run))
    ]
    actions = st.container(horizontal=True)
    if unfinished:
        label = "실패 항목만 재시도" if report and report.failed else f"미완료 {len(unfinished)}개 실행"
        actions.button(label, type="primary", key=K + "retry", on_click=_retry, args=(meeting_id,))
    actions.button("새 회의록 처리", key=K + "new", on_click=_start_over)
    if drafts:
        with st.expander(f"초안으로 남긴 항목 {len(drafts)}개"):
            for item in drafts:
                st.markdown(f"- {item.task} — {item.owner_name or '담당 미정'}")


# --- page ------------------------------------------------------------------------

st.title("회의록 → 액션")
st.caption(
    "회의록을 올리면 요약·결정사항·액션 아이템을 추출합니다. "
    "확인하고 승인한 항목만 Google Calendar에 등록하고 Slack으로 알립니다."
)
with st.container(horizontal=True):
    if settings.actions_dry_run:
        st.badge("DRY_RUN — 실제 등록·발송 없음", color="orange")
    else:
        st.badge("실제 실행", color="red")
    st.badge(f"저장소: {settings.state_backend}", color="gray")
    st.badge("모델: " + (" → ".join(settings.gemini_models) or "미지정"), color="blue")

if not ss.get(K + "current_id"):
    requested = st.query_params.get("meeting")
    if requested and store.get_meeting(requested):
        ss[K + "current_id"] = requested

if ss.get(K + "current_id"):
    if st.query_params.get("meeting") != ss[K + "current_id"]:
        st.query_params["meeting"] = ss[K + "current_id"]
    _render_approved(ss[K + "current_id"])
elif K + "loaded" not in ss:
    _render_source_picker()
else:
    _render_loaded(ss[K + "loaded"])
    if K + "result" in ss:
        _render_review(ss[K + "result"])
