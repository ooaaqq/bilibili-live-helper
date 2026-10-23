import asyncio

import pytest
from curl_cffi.requests.errors import RequestsError

from bilibili_live_helper.bilibili import BilibiliClient, BilibiliError, _is_live
from bilibili_live_helper.models import LiveRoom


def test_recognizes_only_active_live_status():
    assert _is_live(1)
    assert _is_live("1")
    assert _is_live(True)
    assert not _is_live(0)
    assert not _is_live(2)


@pytest.mark.asyncio
async def test_discovers_live_rooms_in_whitelist_order():
    client = BilibiliClient("key", 10, 1)

    async def fake_get(*_args, **_kwargs):
        return {
            "2": {
                "room_id": 202,
                "uname": "Beta",
                "live_status": 1,
                "area_v2_id": 9,
                "area_v2_parent_id": 6,
            },
            "1": {"room_id": 101, "uname": "Alpha", "live_status": 0},
            "3": {"room_id": 303, "uname": "Gamma", "live_status": 2},
        }

    client._get = fake_get
    rooms = await client.discover_live_rooms((1, 2, 3))

    assert [(room.anchor_id, room.room_id, room.anchor_name) for room in rooms] == [
        (2, 202, "Beta")
    ]
    assert rooms[0].area_id == 9
    assert rooms[0].parent_area_id == 6


@pytest.mark.asyncio
async def test_get_retries_curl_errors_but_post_does_not():
    sleeps: list[float] = []

    async def no_sleep(seconds: float):
        sleeps.append(seconds)

    client = BilibiliClient("key", 10, 1, sleep=no_sleep)
    get_session = FakeSession(
        [
            RequestsError("secret URL must not escape", code=7),
            FakeResponse({"code": 0, "data": {"ok": True}}),
        ]
    )
    client.session = get_session

    assert await client._get("https://example.invalid/read", signed=False) == {
        "ok": True
    }
    assert get_session.calls == 2

    post_session = FakeSession([RequestsError("contains access_key=secret", code=28)])
    client.session = post_session
    with pytest.raises(BilibiliError) as captured:
        await client._post("https://example.invalid/write", {"value": 1})
    assert post_session.calls == 1
    assert captured.value.ambiguous
    assert "access_key" not in str(captured.value)
    assert "secret" not in str(captured.value)
    assert sleeps


@pytest.mark.asyncio
async def test_api_rejection_is_definitive_but_invalid_post_response_is_ambiguous():
    client = BilibiliClient("key", 10, 1, sleep=_no_sleep)
    client.session = FakeSession(
        [
            FakeResponse({"code": 10030, "message": "denied"}),
            FakeResponse({"unexpected": True}),
        ]
    )

    with pytest.raises(BilibiliError) as rejected:
        await client._post("https://example.invalid/write", {"value": 1})
    assert rejected.value.code == 10030
    assert not rejected.value.ambiguous

    with pytest.raises(BilibiliError) as invalid:
        await client._post("https://example.invalid/write", {"value": 1})
    assert invalid.value.ambiguous


@pytest.mark.asyncio
async def test_request_lock_limits_in_flight_requests_to_one():
    client = BilibiliClient("key", 10, 1, sleep=_no_sleep)
    session = FakeSession(
        [FakeResponse({"code": 0, "data": {}}), FakeResponse({"code": 0, "data": {}})]
    )
    client.session = session

    await asyncio.gather(
        client._get("https://example.invalid/one", signed=False),
        client._get("https://example.invalid/two", signed=False),
    )

    assert session.max_active == 1


@pytest.mark.asyncio
async def test_heartbeat_uses_room_area_and_stable_session_identity():
    now = 1_788_000_000.0
    client = BilibiliClient("key", 10, 1, wall_time=lambda: now)
    room = LiveRoom(1, 101, "Alpha", "", 371, 9)
    session = client.new_heartbeat_session(room)
    payloads = []

    async def fake_post(_url, data, **_kwargs):
        payloads.append(data)
        return {}

    client._post = fake_post
    await client.heartbeat(room, session, 60)
    await client.heartbeat(room, session, 60)

    assert session.uuid != session.click_id
    assert {payload["uuid"] for payload in payloads} == {session.uuid}
    assert {payload["click_id"] for payload in payloads} == {session.click_id}
    assert {payload["area_id"] for payload in payloads} == {"371"}
    assert {payload["parent_id"] for payload in payloads} == {"9"}
    assert {payload["watch_time"] for payload in payloads} == {"60"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"code": -101, "message": "not logged in"}, {"unexpected": True}, {"code": True}],
)
async def test_get_does_not_retry_api_rejection_or_invalid_envelope(payload):
    client = BilibiliClient("key", 10, 1, sleep=_no_sleep)
    client.session = FakeSession([FakeResponse(payload)])
    with pytest.raises(BilibiliError):
        await client._get("https://example.invalid/read", signed=False)
    assert client.session.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_calls", [(429, 1), (403, 1), (503, 2)])
async def test_get_retries_only_transient_http_status(status, expected_calls):
    client = BilibiliClient("key", 10, 1, sleep=_no_sleep)
    response = type("Response", (), {"status_code": status})()
    client.session = FakeSession(
        [
            RequestsError("secret URL", response=response),
            FakeResponse({"code": 0, "data": {"ok": True}}),
        ]
    )
    if expected_calls == 1:
        with pytest.raises(BilibiliError):
            await client._get("https://example.invalid/read", signed=False)
    else:
        assert await client._get("https://example.invalid/read", signed=False) == {
            "ok": True
        }
    assert client.session.calls == expected_calls


@pytest.mark.asyncio
async def test_get_does_not_retry_invalid_json():
    response = FakeResponse(None)
    response.json = lambda: (_ for _ in ()).throw(ValueError("secret response"))
    client = BilibiliClient("key", 10, 1, sleep=_no_sleep)
    client.session = FakeSession([response])
    with pytest.raises(BilibiliError) as captured:
        await client._get("https://example.invalid/read", signed=False)
    assert client.session.calls == 1
    assert "secret" not in str(captured.value)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def request(self, *_args, **_kwargs):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            outcome = next(self.outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.active -= 1


async def _no_sleep(_seconds: float):
    return None
