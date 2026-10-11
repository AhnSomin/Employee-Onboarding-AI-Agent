import pytest
from slack_sdk.errors import SlackApiError

from onboarding_agent.config import Settings
from onboarding_agent.integrations.slack import SlackError, delete_message, get_client, post_message


class FakeWebClient:
    def __init__(self, error: str | None = None):
        self.error = error
        self.posted = []
        self.deleted = []

    def _fail(self):
        if self.error:
            raise SlackApiError("failed", {"ok": False, "error": self.error})

    def chat_postMessage(self, **kwargs):
        self._fail()
        self.posted.append(kwargs)
        return {"ts": "1700000000.000100"}

    def chat_delete(self, **kwargs):
        self._fail()
        self.deleted.append(kwargs)
        return {"ok": True}


def test_post_message_returns_ts_and_passes_thread():
    client = FakeWebClient()
    ts = post_message("C1", "본문", [{"type": "section"}], thread_ts="1.2", client=client)
    assert ts == "1700000000.000100"
    sent = client.posted[0]
    assert (sent["channel"], sent["text"], sent["thread_ts"]) == ("C1", "본문", "1.2")
    assert sent["unfurl_links"] is False


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("not_in_channel", "봇을 채널에 초대하세요."),
        ("invalid_auth", "봇 토큰을 확인하세요."),
        ("channel_not_found", "채널 ID를 확인하세요."),
    ],
)
def test_errors_become_actionable_korean(code, message):
    with pytest.raises(SlackError) as exc_info:
        post_message("C1", "본문", client=FakeWebClient(code))
    assert str(exc_info.value).startswith(message)
    assert exc_info.value.code == code


def test_missing_configuration_is_reported():
    with pytest.raises(SlackError, match="SLACK_CHANNEL_ID"):
        post_message("", "본문", client=FakeWebClient())
    with pytest.raises(SlackError, match="SLACK_BOT_TOKEN"):
        get_client(Settings())


def test_delete_treats_missing_message_as_done():
    assert delete_message("C1", "1.2", client=FakeWebClient()) is True
    assert delete_message("C1", "1.2", client=FakeWebClient("message_not_found")) is False
