"""Configuration and connectivity check for Gemini, Calendar, Sheets and Slack.

Run with: uv run python scripts/smoke_test.py

Prints OK / SKIP (value missing) / FAIL (cause and fix) per integration.
Secret values are never printed, only whether they are set and their first
4 characters. Creates no calendar events and sends no Slack messages.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr

from onboarding_agent.config import (
    REPO_ROOT,
    ConfigError,
    MissingCredential,
    Settings,
    load_service_account_info,
    load_settings,
)

Status = Literal["OK", "SKIP", "FAIL"]
TIMEOUT_SEC = 30
MASKED_VARS = {
    "GEMINI_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GCAL_CALENDAR_ID",
    "GSHEETS_SPREADSHEET_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_ID",
}


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    lines: list[str] = field(default_factory=list)


def mask(value: str | None) -> str:
    if not value:
        return "(없음)"
    return f"{value[:4]}…" if len(value) > 4 else "****"


def describe_settings(settings: Settings) -> list[str]:
    lines = []
    for name in Settings.model_fields:
        key = name.upper()
        value: Any = getattr(settings, name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if key in MASKED_VARS:
            shown = f"설정됨 ({mask(value)})" if value else "(없음)"
        elif value is None or value == []:
            shown = "(없음)"
        elif isinstance(value, bool):
            shown = str(value).lower()
        elif isinstance(value, list):
            shown = ", ".join(value)
        elif isinstance(value, Path) and value.is_relative_to(REPO_ROOT):
            shown = str(value.relative_to(REPO_ROOT))
        else:
            shown = str(value)
        lines.append(f"  {key} = {shown}")
    return lines


# --- Gemini ---


def check_gemini(settings: Settings) -> CheckResult:
    name = "Gemini"
    if settings.gemini_api_key is None:
        return CheckResult(name, "SKIP", "GEMINI_API_KEY 없음")

    import httpx
    from google import genai
    from google.genai import errors, types

    client = genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(
            timeout=TIMEOUT_SEC * 1000, retry_options=types.HttpRetryOptions(attempts=3)
        ),
    )
    try:
        models = sorted(
            (m.name or "").removeprefix("models/")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
        )
    except errors.APIError as exc:
        return CheckResult(name, "FAIL", f"모델 목록 조회 실패 ({exc.code}) — GEMINI_API_KEY를 확인하세요.")
    except httpx.TransportError as exc:
        return CheckResult(name, "FAIL", f"네트워크 오류 ({type(exc).__name__})")

    lines = ["  사용 가능한 모델 (generateContent 지원):", *(f"    - {m}" for m in models)]
    if not settings.gemini_models:
        return CheckResult(
            name,
            "SKIP",
            "모델 미지정 — 아래 목록에서 골라 GEMINI_MODEL_PRIMARY와 GEMINI_MODEL_FALLBACKS에 넣으세요.",
            lines,
        )

    failures = []
    for model in settings.gemini_models:
        try:
            _ping_function_call(client, model)
        except errors.APIError as exc:
            failures.append(f"{model}: {exc.code} {exc.status or ''}".strip())
        except Exception as exc:
            failures.append(f"{model}: {type(exc).__name__}")
        else:
            lines.append(f"  {model}: 함수 호출 응답 확인")
    if failures:
        return CheckResult(
            name,
            "FAIL",
            "지정 모델 호출 실패 — " + "; ".join(failures) + " (모델 이름이 아래 목록에 있는지 확인하세요)",
            lines,
        )
    return CheckResult(name, "OK", f"지정 모델 {len(settings.gemini_models)}개 모두 함수 호출 응답", lines)


def _ping_function_call(client: Any, model: str) -> None:
    from google.genai import types

    declaration = types.FunctionDeclaration(
        name="report_status",
        description="연결 상태를 보고한다.",
        parameters_json_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    )
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=[declaration])],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY
            )
        ),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    response = client.models.generate_content(model=model, contents="상태를 보고해 줘.", config=config)
    if not response.function_calls:
        raise RuntimeError("no function call in response")


# --- Google (Calendar, Sheets) ---


def _service_account(settings: Settings, name: str) -> tuple[dict[str, Any] | None, CheckResult | None]:
    try:
        info = load_service_account_info(settings)
    except MissingCredential as exc:
        return None, CheckResult(name, "SKIP", str(exc))
    except ConfigError as exc:
        return None, CheckResult(name, "FAIL", str(exc))
    if info is None:
        return None, CheckResult(name, "SKIP", "GOOGLE_SERVICE_ACCOUNT_JSON 없음")
    return info, None


def check_calendar(settings: Settings) -> CheckResult:
    name = "Google Calendar"
    calendar_id = settings.gcal_calendar_id
    if not calendar_id:
        return CheckResult(name, "SKIP", "GCAL_CALENDAR_ID 없음")
    info, problem = _service_account(settings, name)
    if problem:
        return problem
    email = info["client_email"]
    if calendar_id in ("primary", email):
        return CheckResult(
            name,
            "FAIL",
            "서비스 계정 자신의 캘린더는 사람에게 보이지 않습니다. 사람 계정으로 만든 캘린더의 ID를 넣으세요.",
        )

    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=TIMEOUT_SEC))
    try:
        service = build("calendar", "v3", http=http, cache_discovery=False)
        calendar = service.calendars().get(calendarId=calendar_id).execute(num_retries=2)
    except HttpError as exc:
        if exc.status_code in (403, 404):
            return CheckResult(
                name,
                "FAIL",
                f"캘린더에 접근할 수 없습니다 ({exc.status_code}). 캘린더를 서비스 계정 이메일({email})에 "
                "'일정 변경' 권한으로 공유했는지, GCAL_CALENDAR_ID가 맞는지 확인하세요.",
            )
        return CheckResult(name, "FAIL", f"Calendar API 오류 ({exc.status_code})")
    except Exception as exc:
        return CheckResult(
            name,
            "FAIL",
            f"인증 또는 네트워크 오류 ({type(exc).__name__}) — 서비스 계정 키와 Calendar API 활성화를 확인하세요.",
        )
    return CheckResult(
        name,
        "OK",
        f"캘린더 '{calendar.get('summary', '')}' 조회 성공 (서비스 계정 {email}). 이벤트는 만들지 않았습니다.",
    )


def check_sheets(settings: Settings) -> CheckResult:
    name = "Google Sheets"
    if not settings.gsheets_spreadsheet_id:
        return CheckResult(name, "SKIP", "GSHEETS_SPREADSHEET_ID 없음")
    info, problem = _service_account(settings, name)
    if problem:
        return problem
    email = info["client_email"]

    import gspread
    from gspread.http_client import BackOffHTTPClient

    share_hint = (
        f"스프레드시트를 서비스 계정 이메일({email})에 편집 권한으로 공유했는지, "
        "GSHEETS_SPREADSHEET_ID가 맞는지 확인하세요."
    )
    try:
        client = gspread.service_account_from_dict(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
            http_client=BackOffHTTPClient,
        )
        client.set_timeout(TIMEOUT_SEC)
        spreadsheet = client.open_by_key(settings.gsheets_spreadsheet_id)
        titles = [worksheet.title for worksheet in spreadsheet.worksheets()]
    except gspread.exceptions.SpreadsheetNotFound:
        return CheckResult(name, "FAIL", "스프레드시트를 찾을 수 없습니다. " + share_hint)
    except gspread.exceptions.APIError as exc:
        code = exc.response.status_code
        if code in (403, 404):
            return CheckResult(name, "FAIL", f"스프레드시트에 접근할 수 없습니다 ({code}). " + share_hint)
        return CheckResult(name, "FAIL", f"Sheets API 오류 ({code}) — Sheets API 활성화를 확인하세요.")
    except Exception as exc:
        return CheckResult(name, "FAIL", f"인증 또는 네트워크 오류 ({type(exc).__name__})")
    return CheckResult(
        name, "OK", f"스프레드시트 '{spreadsheet.title}' 열기 성공, 워크시트: {', '.join(titles)}"
    )


# --- Slack ---


def check_slack(settings: Settings) -> CheckResult:
    name = "Slack"
    if settings.slack_bot_token is None:
        return CheckResult(name, "SKIP", "SLACK_BOT_TOKEN 없음")

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    from slack_sdk.http_retry.builtin_handlers import (
        ConnectionErrorRetryHandler,
        RateLimitErrorRetryHandler,
    )

    client = WebClient(
        token=settings.slack_bot_token.get_secret_value(),
        timeout=TIMEOUT_SEC,
        retry_handlers=[
            ConnectionErrorRetryHandler(max_retry_count=2),
            RateLimitErrorRetryHandler(max_retry_count=2),
        ],
    )
    try:
        auth = client.auth_test()
    except SlackApiError as exc:
        error = exc.response.get("error")
        if error in ("invalid_auth", "not_authed", "token_revoked", "account_inactive"):
            return CheckResult(name, "FAIL", f"인증 실패 ({error}) — 봇 토큰을 확인하세요.")
        return CheckResult(name, "FAIL", f"auth.test 실패 ({error})")
    except Exception as exc:
        return CheckResult(name, "FAIL", f"네트워크 오류 ({type(exc).__name__})")

    bot = f"봇 {auth.get('user')} / 워크스페이스 {auth.get('team')}"
    if not settings.slack_channel_id:
        return CheckResult(name, "SKIP", f"auth.test 성공 ({bot}). SLACK_CHANNEL_ID 없음")

    try:
        channel = client.conversations_info(channel=settings.slack_channel_id)["channel"]
    except SlackApiError as exc:
        error = exc.response.get("error")
        if error == "missing_scope":
            return CheckResult(
                name,
                "OK",
                f"auth.test 성공 ({bot}). 채널 확인에는 channels:read 스코프가 필요해 건너뛰었습니다 — "
                "봇이 채널에 초대됐는지 직접 확인하세요.",
            )
        if error == "channel_not_found":
            return CheckResult(
                name, "FAIL", "채널 ID를 확인하세요. 비공개 채널이면 봇을 먼저 초대해야 합니다."
            )
        return CheckResult(name, "FAIL", f"채널 확인 실패 ({error})")
    except Exception as exc:
        return CheckResult(name, "FAIL", f"네트워크 오류 ({type(exc).__name__})")
    if channel.get("is_member") is False:
        return CheckResult(name, "FAIL", f"봇을 채널 #{channel.get('name')}에 초대하세요.")
    return CheckResult(name, "OK", f"{bot}, 채널 #{channel.get('name')} 접근 확인. 메시지는 보내지 않았습니다.")


CHECKS: list[tuple[str, Callable[[Settings], CheckResult]]] = [
    ("Gemini", check_gemini),
    ("Google Calendar", check_calendar),
    ("Google Sheets", check_sheets),
    ("Slack", check_slack),
]


def run_checks(settings: Settings) -> list[CheckResult]:
    results = []
    for name, check in CHECKS:
        try:
            results.append(check(settings))
        except Exception as exc:  # one broken check must not hide the others
            results.append(CheckResult(name, "FAIL", f"예상치 못한 오류 ({type(exc).__name__})"))
    return results


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[FAIL] 설정 — {exc}")
        return 1

    print("설정 (비밀값과 ID는 앞 4자리만 표시)")
    for line in describe_settings(settings):
        print(line)
    print()

    results = run_checks(settings)
    for result in results:
        print(f"[{result.status}] {result.name} — {result.detail}")
        for line in result.lines:
            print(line)
    counts = {status: sum(r.status == status for r in results) for status in ("OK", "SKIP", "FAIL")}
    print(f"\n결과: OK {counts['OK']} · SKIP {counts['SKIP']} · FAIL {counts['FAIL']}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
