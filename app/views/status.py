"""액션 현황 page (spec 12).

Lists action items by meeting and status, marks approved items done or
cancelled (the reminder batch then skips them), and previews the reminders a
batch run would send at a chosen time. The preview sends and records nothing;
GitHub Actions does the sending. Session keys use the "meeting.status." prefix.
"""

from datetime import datetime, time

import streamlit as st

from onboarding_agent.actions.dry_run import DRY_RUN_PREFIX
from onboarding_agent.actions.item_status import mark_cancelled, mark_done
from onboarding_agent.config import ConfigError, get_settings
from onboarding_agent.meeting.dates import format_due
from onboarding_agent.meeting.models import ActionItem
from onboarding_agent.meeting.render import STAGE_LABELS
from onboarding_agent.scheduler.reminders import plan_reminders
from onboarding_agent.store import StoreUnavailable, get_store

KEY = "meeting.status."
STATUS_LABELS = {"draft": "초안", "approved": "진행 중", "done": "완료", "cancelled": "취소"}

st.title("액션 현황")
st.caption(
    "승인한 액션 아이템의 진행 상황을 보고 완료·취소를 처리합니다. 완료·취소한 항목에는 리마인더를 보내지 않습니다."
)

try:
    settings = get_settings()
    store = get_store(settings)
except (ConfigError, StoreUnavailable) as exc:
    st.error(str(exc))
    st.stop()


def due_label(item: ActionItem) -> str:
    return format_due(item.due_date, item.due_time) if item.due_date else "미정"


def last_reminder(item: ActionItem) -> str:
    if item.reminders:
        log = item.reminders[-1]
        label = f"{STAGE_LABELS[log.stage]} ({log.sent_at.astimezone(settings.tz):%m/%d %H:%M})"
        return label + (" · DRY_RUN" if log.ts.startswith(DRY_RUN_PREFIX) else "")
    return STAGE_LABELS[item.last_reminded_stage] if item.last_reminded_stage else "-"


def apply_status(action: str) -> None:
    selected = st.session_state.get(f"{KEY}selected", [])
    if action == "done":
        changed = mark_done(store, selected, datetime.now(settings.tz))
        st.session_state[f"{KEY}flash"] = f"{len(changed)}건을 완료 처리했습니다."
    else:
        changed = mark_cancelled(store, selected)
        st.session_state[f"{KEY}flash"] = f"{len(changed)}건을 취소 처리했습니다."
    st.session_state[f"{KEY}selected"] = []


if flash := st.session_state.pop(f"{KEY}flash", None):
    st.success(flash)

meetings = {m.meeting_id: f"{m.title} ({m.meeting_date:%m/%d})" for m in store.list_meetings()}

left, right = st.columns(2)
meeting_id = left.selectbox(
    "회의",
    options=[None, *meetings],
    format_func=lambda mid: "전체" if mid is None else meetings[mid],
    key=f"{KEY}meeting",
)
statuses = right.multiselect(
    "상태",
    options=list(STATUS_LABELS),
    default=["approved"],
    format_func=STATUS_LABELS.get,
    key=f"{KEY}statuses",
)

items = [
    item
    for item in store.list_items(meeting_id=meeting_id)
    if not statuses or item.status in statuses
]
if not items:
    st.info("표시할 액션 아이템이 없습니다.")
else:
    st.dataframe(
        [
            {
                "할 일": item.task,
                "담당자": item.owner_name or "미확정",
                "기한": item.due_date,
                "시간": f"{item.due_time:%H:%M}" if item.due_time else "",
                "상태": STATUS_LABELS[item.status],
                "마지막 리마인더": last_reminder(item),
            }
            for item in items
        ],
        hide_index=True,
    )

    approved = {
        item.item_id: f"{item.task} — {item.owner_name or '담당 미정'} · {due_label(item)}"
        for item in items
        if item.status == "approved"
    }
    if approved:
        # Drop choices that are no longer listed before the widget is drawn.
        st.session_state[f"{KEY}selected"] = [
            i for i in st.session_state.get(f"{KEY}selected", []) if i in approved
        ]
        st.multiselect(
            "처리할 항목",
            options=list(approved),
            format_func=approved.get,
            key=f"{KEY}selected",
            placeholder="진행 중인 항목을 고르세요",
        )
        chosen = bool(st.session_state[f"{KEY}selected"])
        with st.container(horizontal=True):
            st.button("완료 처리", key=f"{KEY}done", type="primary", disabled=not chosen,
                      on_click=apply_status, args=("done",))
            st.button("취소 처리", key=f"{KEY}cancel", disabled=not chosen, on_click=apply_status, args=("cancel",))

st.subheader("리마인더 미리보기")
st.caption(
    "기준 시각에 리마인더 배치가 돌면 보낼 알림입니다. 여기서는 보내거나 기록하지 않습니다. "
    "실제 발송은 GitHub Actions가 평일 09:00에 합니다."
)
today = datetime.now(settings.tz).date()
day_col, time_col = st.columns(2)
day = day_col.date_input("기준 날짜", value=today, key=f"{KEY}preview_day")
at = time_col.time_input("기준 시각", value=time(9, 0), key=f"{KEY}preview_time")
plan = plan_reminders(
    store, datetime.combine(day, at, tzinfo=settings.tz), settings=settings, dry_run=settings.actions_dry_run
)
if settings.actions_dry_run:
    st.caption("DRY_RUN 설정이라 DRY_RUN으로 보낸 기록도 이미 보낸 것으로 봅니다.")
if not plan:
    st.info("이 시각에 보낼 리마인더가 없습니다.")
for reminder in plan:
    where = "회의 요약 스레드에 답글" if reminder.thread_ts else "새 메시지"
    st.markdown(f"**[{STAGE_LABELS[reminder.stage]}]** {reminder.item.task} · {where}")
    st.code(reminder.message.text, language=None)
