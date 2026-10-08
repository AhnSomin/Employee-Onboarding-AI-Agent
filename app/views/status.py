"""액션 현황 page.

Skeleton: read-only list of action items from the state store. Complete and
cancel actions and the reminder preview are added in a later phase.
Session keys use the "meeting.status." prefix.
"""

import streamlit as st

from onboarding_agent.config import ConfigError, get_settings
from onboarding_agent.store import StoreUnavailable, get_store

KEY = "meeting.status."
STATUS_LABELS = {"draft": "초안", "approved": "진행 중", "done": "완료", "cancelled": "취소"}

st.title("액션 현황")
st.caption("승인한 액션 아이템의 진행 상황을 확인합니다.")

try:
    store = get_store(get_settings())
except (ConfigError, StoreUnavailable) as exc:
    st.error(str(exc))
    st.stop()

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
    st.stop()

st.dataframe(
    [
        {
            "할 일": item.task,
            "담당자": item.owner_name or "미확정",
            "기한": item.due_date,
            "시간": item.due_time,
            "상태": STATUS_LABELS[item.status],
            "마지막 리마인더": item.last_reminded_stage or "-",
        }
        for item in items
    ],
    hide_index=True,
)
