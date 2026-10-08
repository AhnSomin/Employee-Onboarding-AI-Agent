import base64
import json

import pytest

from onboarding_agent.config import (
    REPO_ROOT,
    ConfigError,
    MissingCredential,
    Settings,
    load_service_account_info,
    load_settings,
)


def load(environ, tmp_path, secrets=None, dotenv_text=None):
    dotenv = tmp_path / ".env"
    if dotenv_text is not None:
        dotenv.write_text(dotenv_text, encoding="utf-8")
    return load_settings(environ=environ, dotenv_path=dotenv, secrets=secrets or {})


def test_defaults_without_any_source(tmp_path):
    settings = load({}, tmp_path)
    assert settings.state_backend == "sqlite"
    assert settings.actions_dry_run is True
    assert settings.force_fallback is False
    assert settings.reminder_stages == ["D-1", "D-day", "overdue"]
    assert settings.sqlite_path == REPO_ROOT / "data" / "state.db"
    assert settings.gemini_models == []
    assert str(settings.tz) == "Asia/Seoul"


def test_lookup_order_is_env_then_dotenv_then_secrets(tmp_path):
    settings = load(
        {"GEMINI_MODEL_PRIMARY": "from-env", "SLACK_CHANNEL_ID": ""},
        tmp_path,
        secrets={"SLACK_CHANNEL_ID": "C-secrets", "GCAL_CALENDAR_ID": "cal-secrets"},
        dotenv_text="GEMINI_MODEL_PRIMARY=from-dotenv\nSLACK_CHANNEL_ID=C-dotenv\n",
    )
    assert settings.gemini_model_primary == "from-env"
    assert settings.slack_channel_id == "C-dotenv"  # empty env var falls through
    assert settings.gcal_calendar_id == "cal-secrets"


def test_parses_lists_bools_and_paths(tmp_path):
    settings = load(
        {
            "GEMINI_MODEL_PRIMARY": "a",
            "GEMINI_MODEL_FALLBACKS": " b, a ,, c ",
            "ACTIONS_DRY_RUN": "false",
            "FORCE_FALLBACK": "TRUE",
            "SQLITE_PATH": "tmp/x.db",
            "ROSTER_PATH": str(tmp_path / "roster.yaml"),
        },
        tmp_path,
    )
    assert settings.gemini_model_fallbacks == ["b", "a", "c"]
    assert settings.gemini_models == ["a", "b", "c"]
    assert settings.actions_dry_run is False
    assert settings.force_fallback is True
    assert settings.sqlite_path == REPO_ROOT / "tmp" / "x.db"
    assert settings.roster_path == tmp_path / "roster.yaml"


def test_invalid_value_names_variable_without_echoing_it(tmp_path):
    with pytest.raises(ConfigError) as exc_info:
        load({"STATE_BACKEND": "postgres-xyz", "TIMEZONE": "Mars/Base"}, tmp_path)
    message = str(exc_info.value)
    assert "STATE_BACKEND" in message and "TIMEZONE" in message
    assert "postgres-xyz" not in message


def test_secret_values_stay_out_of_repr(tmp_path):
    settings = load({"GEMINI_API_KEY": "AIzaTOPSECRET", "SLACK_BOT_TOKEN": "xoxb-TOPSECRET"}, tmp_path)
    assert "TOPSECRET" not in repr(settings)
    assert settings.gemini_api_key.get_secret_value() == "AIzaTOPSECRET"


SERVICE_ACCOUNT = {"type": "service_account", "client_email": "bot@demo.iam.gserviceaccount.com"}


@pytest.mark.parametrize("form", ["json", "file", "base64"])
def test_service_account_accepts_json_file_or_base64(tmp_path, form):
    text = json.dumps(SERVICE_ACCOUNT)
    if form == "json":
        value = text
    elif form == "file":
        path = tmp_path / "sa.json"
        path.write_text(text, encoding="utf-8")
        value = str(path)
    else:
        value = base64.b64encode(text.encode()).decode()
    assert load_service_account_info(Settings(google_service_account_json=value)) == SERVICE_ACCOUNT


def test_service_account_unset_missing_or_invalid():
    assert load_service_account_info(Settings()) is None
    with pytest.raises(MissingCredential):
        load_service_account_info(Settings(google_service_account_json="secrets/absent.json"))
    with pytest.raises(ConfigError):
        load_service_account_info(Settings(google_service_account_json="{not json"))
    with pytest.raises(ConfigError):
        load_service_account_info(Settings(google_service_account_json='{"type": "x"}'))
