import pytest
from curl_cffi.requests.errors import RequestsError

from bilibili_live_helper.notify import (
    NtfyNotifier,
    NotificationError,
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

    notifier = NtfyNotifier("https://ntfy.example/topic")
    notifier.session = FailingSession()

    with pytest.raises(NotificationError, match="RequestException"):
        await notifier.publish("Title", "Body", tags="eyes", sequence_id="daily-1")
