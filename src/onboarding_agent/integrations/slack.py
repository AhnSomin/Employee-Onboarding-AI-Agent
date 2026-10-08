"""Generic Slack sender shared by both features.

Only `chat:write` is needed: posting, and deleting the bot's own messages.
Errors become SlackError with a Korean message a person can act on.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)

from ..config import Settings, get_settings

TIMEOUT_SEC = 30

_FRIENDLY = {
    "not_in_channel": "봇을 채널에 초대하세요.",
    "channel_not_found": "채널 ID를 확인하세요. 비공개 채널이면 봇을 먼저 초대해야 합니다.",
    "invalid_auth": "봇 토큰을 확인하세요.",
    "not_authed": "봇 토큰을 확인하세요.",
    "token_revoked": "봇 토큰이 취소되었습니다. 새 토큰을 넣어 주세요.",
    "account_inactive": "봇 토큰이 비활성 상태입니다. 앱 설치를 확인하세요.",
    "missing_scope": "봇에 chat:write 권한이 있는지 확인하세요.",
    "is_archived": "보관된 채널에는 보낼 수 없습니다.",
    "msg_too_long": "메시지가 너무 깁니다.",
}


class SlackError(RuntimeError):
    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _friendly(exc: SlackApiError) -> SlackError:
    code = exc.response.get("error") if exc.response is not None else None
    return SlackError(_FRIENDLY.get(code or "", f"Slack 오류({code})"), code)


@lru_cache(maxsize=4)
def _client_for(token: str) -> WebClient:
    return WebClient(
        token=token,
        timeout=TIMEOUT_SEC,
        retry_handlers=[
            ConnectionErrorRetryHandler(max_retry_count=2),
            RateLimitErrorRetryHandler(max_retry_count=2),
        ],
    )


def get_client(settings: Settings | None = None) -> WebClient:
    settings = settings or get_settings()
    if settings.slack_bot_token is None:
        raise SlackError("SLACK_BOT_TOKEN이 설정되지 않았습니다.", "not_configured")
    return _client_for(settings.slack_bot_token.get_secret_value())


def post_message(
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
    *,
    client: WebClient | None = None,
) -> str:
    """Post a message (or a thread reply) and return its ts."""
    if not channel:
        raise SlackError("SLACK_CHANNEL_ID가 설정되지 않았습니다.", "not_configured")
    client = client or get_client()
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as exc:
        raise _friendly(exc) from None
    except Exception as exc:  # connection errors after retries
        raise SlackError(f"Slack에 연결하지 못했습니다 ({type(exc).__name__}).") from None
    return response["ts"]


def delete_message(channel: str, ts: str, *, client: WebClient | None = None) -> bool:
    """Delete one of the bot's own messages. Returns False when it is already gone."""
    client = client or get_client()
    try:
        client.chat_delete(channel=channel, ts=ts)
    except SlackApiError as exc:
        if exc.response is not None and exc.response.get("error") == "message_not_found":
            return False
        raise _friendly(exc) from None
    return True
