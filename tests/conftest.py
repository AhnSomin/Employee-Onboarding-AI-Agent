"""Test isolation: no real .env or st.secrets, no real logs or state DB, fresh caches."""

import pytest

from onboarding_agent import config, metrics
from onboarding_agent.llm import client as llm_client


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    for name in config.env_var_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path / "absent.env")
    monkeypatch.setattr(config, "_streamlit_secrets", lambda: {})
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(metrics, "LOG_PATH", tmp_path / "logs" / "events.jsonl")
    config.reset_settings_cache()
    llm_client.reset_client()
    yield
    config.reset_settings_cache()
    llm_client.reset_client()
