"""회의록 → 액션 page.

Skeleton: input widgets and run-mode badges only. Extraction, review and
approval are wired in later phases. Session keys use the "meeting." prefix.
"""

import streamlit as st

from onboarding_agent.config import ConfigError, get_settings

KEY = "meeting."

st.title("회의록 → 액션")
st.caption(
    "회의록을 올리면 요약·결정사항·액션 아이템을 추출합니다. "
    "확인하고 승인한 항목만 Google Calendar에 등록하고 Slack으로 알립니다."
)

try:
    settings = get_settings()
except ConfigError as exc:
    st.error(str(exc))
    st.stop()

with st.container(horizontal=True):
    if settings.actions_dry_run:
        st.badge("DRY_RUN — 실제 등록·발송 없음", color="orange")
    else:
        st.badge("실제 실행", color="red")
    st.badge(f"저장소: {settings.state_backend}", color="gray")
    if settings.force_fallback:
        st.badge("규칙 기반 추출 강제", color="violet")

st.file_uploader("회의록 파일 (.txt, .md)", type=["txt", "md"], key=f"{KEY}upload")
st.text_area("또는 회의록 붙여넣기", key=f"{KEY}pasted", height=240)
st.button("추출", key=f"{KEY}extract", type="primary", disabled=True, help="추출 기능은 준비 중입니다.")
st.info("추출·검토·승인 기능은 준비 중입니다.")
