import importlib.util
import sys

import pytest

from onboarding_agent.config import REPO_ROOT, Settings


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_test", REPO_ROOT / "scripts" / "smoke_test.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_test"] = module  # dataclasses look up their module here
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("smoke_test", None)


def test_all_integrations_skip_without_credentials(smoke):
    results = smoke.run_checks(Settings())
    assert [r.name for r in results] == ["Gemini", "Google Calendar", "Google Sheets", "Slack"]
    assert {r.status for r in results} == {"SKIP"}


def test_missing_service_account_file_is_skip(smoke):
    settings = Settings(
        gcal_calendar_id="calendar",
        gsheets_spreadsheet_id="sheet",
        google_service_account_json="secrets/absent.json",
    )
    results = {r.name: r.status for r in smoke.run_checks(settings)}
    assert results["Google Calendar"] == "SKIP"
    assert results["Google Sheets"] == "SKIP"


def test_service_account_own_calendar_is_rejected(smoke, tmp_path):
    key = tmp_path / "sa.json"
    key.write_text('{"client_email": "bot@demo.iam.gserviceaccount.com"}', encoding="utf-8")
    result = smoke.check_calendar(
        Settings(gcal_calendar_id="primary", google_service_account_json=str(key))
    )
    assert result.status == "FAIL"


def test_secrets_and_ids_are_masked(smoke):
    lines = "\n".join(
        smoke.describe_settings(
            Settings(gemini_api_key="AIzaTOPSECRET", slack_channel_id="C0123456789")
        )
    )
    assert "TOPSECRET" not in lines
    assert "AIza…" in lines
    assert "C0123456789" not in lines
    assert smoke.mask("abc") == "****"
