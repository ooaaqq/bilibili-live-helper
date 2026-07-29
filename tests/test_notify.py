import pytest
from curl_cffi.requests.errors import RequestsError

from bilibili_live_helper.notify import (
    NotificationError,
    NtfyNotifier,
    validate_sequence_id,
)


@pytest.mark.parametrize(
    "sequence_id",
    ["bilibili-2026-07-11-1", "bilibili_watch_2026-07-11"],
)
def test_accepts_ntfy_safe_sequence_ids(sequence_id):
    validate_sequence_id(sequence_id)


@pytest.mark.parametrize(
    "sequence_id", ["fans-medal:2026-07-11:1", "", "contains space"]
)
def test_rejects_ntfy_unsafe_sequence_ids(sequence_id):
    with pytest.raises(ValueError):
        validate_sequence_id(sequence_id)


@pytest.mark.asyncio
async def test_notifier_wraps_transport_failures():
    class FailingSession:
        async def post(self, *_args, **_kwargs):
            raise RequestsError("network failed")

    notifier = NtfyNotifier("https://ntfy.example", "topic")
    notifier.session = FailingSession()

    with pytest.raises(NotificationError, match="RequestException"):
        await notifier.publish("Title", "Body", tags="eyes", sequence_id="daily-1")


@pytest.mark.asyncio
async def test_notifier_sends_utf8_payload_without_user_text_headers():
    class Response:
        def raise_for_status(self):
            pass

    class RecordingSession:
        def __init__(self):
            self.request: tuple[str, dict[str, object], dict[str, str]] | None = None

        async def post(self, url, *, json, headers):
            self.request = (url, json, headers)
            return Response()

    notifier = NtfyNotifier("https://ntfy.example", "notifications")
    session = RecordingSession()
    notifier.session = session

    await notifier.publish(
        "\u300c\u4e2d\u6587\u4e3b\u64ad\u300d \u4efb\u52a1\u5b8c\u6210",
        "\U0001f389 \u4efb\u52a1\u5df2\u5b8c\u6210",
        tags="white_check_mark,rocket",
        sequence_id="daily-1",
    )

    assert session.request is not None
    url, payload, headers = session.request
    assert url == "https://ntfy.example"
    assert headers == {}
    assert payload == {
        "topic": "notifications",
        "title": "\u300c\u4e2d\u6587\u4e3b\u64ad\u300d \u4efb\u52a1\u5b8c\u6210",
        "message": "\U0001f389 \u4efb\u52a1\u5df2\u5b8c\u6210",
        "tags": ["white_check_mark", "rocket"],
        "sequence_id": "daily-1",
    }
