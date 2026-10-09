"""Streamlit entrypoint and navigation shared by both features.

Run with: uv run streamlit run app/main.py
Each feature owns its pages under app/views/.
"""

import streamlit as st

st.set_page_config(page_title="신입사원 온보딩 AI Agent", page_icon="🧭", layout="wide")

navigation = st.navigation(
    [
        st.Page("views/regulations.py", title="규정 Q&A", icon="💬", url_path="qa", default=True),
        st.Page("views/meeting.py", title="회의록 → 액션", icon="📋", url_path="meeting"),
        st.Page("views/status.py", title="액션 현황", icon="✅", url_path="status"),
    ]
)
navigation.run()
