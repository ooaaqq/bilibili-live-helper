import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as clock_time
from typing import Any, Self
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from .models import LiveRoom

APP_KEY = "4409e2ce8ffd12b8"
APP_SECRET = "59b43e04ad6965f34319062b478f83dd"
APP_HEADERS = {
    "User-Agent": "Mozilla/5.0 BiliDroid/6.73.1 (bbcallen@gmail.com) os/android model/Mi 10 Pro mobi_app/android build/6731100 channel/xiaomi innerVer/6731110 osVer/12 network/2",
    "Content-Type": "application/x-www-form-urlencoded",
}
EMOTES = ("[花]", "[比心]")
SHANGHAI = ZoneInfo("Asia/Shanghai")
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
Params = Mapping[str, Any] | Sequence[tuple[str, Any]]


class BilibiliError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        ambiguous: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class HeartbeatSession:
    uuid: str
    click_id: str
    up_session: str


@dataclass
class BilibiliClient:
    access_key: str
    timeout_seconds: int
    request_interval_seconds: int
    sleep: Sleep = asyncio.sleep
    monotonic: Clock = time.monotonic
    wall_time: Clock = time.time
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def __post_init__(self) -> None:
        self.session: AsyncSession | None = None
        self.mid = 0
        self.buvid3 = ""
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def __aenter__(self) -> Self:
        self.session = AsyncSession(max_clients=1, impersonate="chrome131")
        try:
            await self.login()
        except Exception:
            await self.session.close()
            self.session = None
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def login(self) -> None:
        account = _mapping(
            await self._get("https://app.bilibili.com/x/v2/account/mine", signed=True),
            "account",
        )
        self.mid = _positive_int(account.get("mid"), "account.mid")
        fingerprint = _mapping(
            await self._get(
                "https://api.bilibili.com/x/frontend/finger/spi", signed=False
            ),
            "fingerprint",
        )
        self.buvid3 = fingerprint.get("b_3", "")
        if not isinstance(self.buvid3, str) or not self.buvid3:
            raise BilibiliError("Bilibili fingerprint response did not contain buvid3")

    async def discover_live_rooms(self, anchor_ids: Sequence[int]) -> list[LiveRoom]:
        params = [("uids[]", anchor_id) for anchor_id in anchor_ids]
        payload = _mapping(
            await self._get(
                "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
                params,
                signed=False,
            ),
            "live status",
        )
        rooms: list[LiveRoom] = []
        for anchor_id in anchor_ids:
            value = payload.get(str(anchor_id))
            if not isinstance(value, dict) or not _is_live(value.get("live_status")):
                continue
            room_id = _positive_int(value.get("room_id"), f"room {anchor_id}.room_id")
            anchor_name = value.get("uname")
            rooms.append(
                LiveRoom(
                    anchor_id=anchor_id,
                    room_id=room_id,
                    anchor_name=anchor_name
                    if isinstance(anchor_name, str) and anchor_name
                    else str(anchor_id),
                    title=value.get("title")
                    if isinstance(value.get("title"), str)
                    else "",
                    area_id=_non_negative_int(
                        value.get("area_v2_id", 0), f"room {anchor_id}.area_v2_id"
                    ),
                    parent_area_id=_non_negative_int(
                        value.get("area_v2_parent_id", 0),
                        f"room {anchor_id}.area_v2_parent_id",
                    ),
                )
            )
        return rooms

    async def like(self, room: LiveRoom, click_count: int) -> None:
        await self._post(
            "https://api.live.bilibili.com/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3",
            {
                "click_time": click_count,
                "room_id": room.room_id,
                "anchor_id": room.anchor_id,
                "uid": self.mid,
            },
            headers=self._headers(),
        )

    def new_heartbeat_session(self, room: LiveRoom) -> HeartbeatSession:
        return HeartbeatSession(
            uuid=str(uuid.uuid4()),
            click_id=str(uuid.uuid4()),
            up_session=f"l:one:live:record:{room.room_id}:{int(self.wall_time()) - 88888}",
        )

    async def heartbeat(
        self, room: LiveRoom, session: HeartbeatSession, watch_seconds: int
    ) -> None:
        now = int(self.wall_time())
        local_now = datetime.fromtimestamp(now, SHANGHAI)
        start_of_day = int(
            datetime.combine(local_now.date(), clock_time(), SHANGHAI).timestamp()
        )
        started_at = max(start_of_day, now - watch_seconds)
        payload = {
            "platform": "android",
            "uuid": session.uuid,
            "buvid": _random_token(37).upper(),
            "seq_id": "1",
            "room_id": str(room.room_id),
            "parent_id": str(room.parent_area_id or 6),
            "area_id": str(room.area_id or 283),
            "timestamp": str(started_at),
            "secret_key": "axoaadsffcazxksectbbb",
            "watch_time": str(now - started_at),
            "up_id": str(room.anchor_id),
            "up_level": "40",
            "jump_from": "30000",
            "gu_id": _random_token(43).lower(),
            "play_type": "0",
            "play_url": "",
            "s_time": "0",
            "data_behavior_id": "",
            "data_source_id": "",
            "up_session": session.up_session,
            "visit_id": _random_token(32).lower(),
            "watch_status": "%7B%22pk_id%22%3A0%2C%22screen_status%22%3A1%7D",
            "click_id": session.click_id,
            "session_id": "",
            "player_type": "0",
            "client_ts": str(now),
        }
        payload["client_sign"] = _client_sign(payload)
        await self._post(
            "https://live-trace.bilibili.com/xlive/data-interface/v1/heartbeat/mobileHeartBeat",
            payload,
        )

    async def send_danmaku(self, room: LiveRoom) -> str:
        message = random.choice(EMOTES)
        payload = _mapping(
            await self._post(
                "https://api.live.bilibili.com/xlive/app-room/v1/dM/sendmsg",
                {
                    "cid": room.room_id,
                    "msg": message,
                    "rnd": int(self.wall_time()),
                    "color": "16777215",
                    "fontsize": "25",
                },
            ),
            "danmaku response",
            ambiguous=True,
        )
        extra = (
            payload.get("mode_info", {}).get("extra")
            if isinstance(payload.get("mode_info"), dict)
            else None
        )
        if not isinstance(extra, str) or not extra:
            return message
        try:
            content = json.loads(extra).get("content")
        except json.JSONDecodeError, AttributeError:
            return message
        return content if isinstance(content, str) and content else message

    async def _get(
        self, url: str, params: Params | None = None, *, signed: bool
    ) -> Any:
        if signed:
            if params is not None and not isinstance(params, Mapping):
                raise TypeError("Signed parameters must be a mapping")
            params = self._signed(params or {})
        return await self._request("GET", url, attempts=3, params=params or {})

    async def _post(
        self,
        url: str,
        data: Mapping[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return await self._request(
            "POST",
            url,
            attempts=1,
            data=self._signed(data),
            headers=headers or dict(APP_HEADERS),
        )

    async def _request(
        self, method: str, url: str, *, attempts: int, **kwargs: Any
    ) -> Any:
        if not self.session:
            raise RuntimeError("Bilibili client is not started")
        endpoint = urlsplit(url).path
        last_error: BilibiliError | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self._request_lock:
                    delay = max(0.0, self._next_request_at - self.monotonic())
                    await self.sleep(delay)
                    try:
                        response = await self.session.request(
                            method, url, timeout=self.timeout_seconds, **kwargs
                        )
                        response.raise_for_status()
                        body = response.json()
                    finally:
                        self._next_request_at = (
                            self.monotonic() + self.request_interval_seconds
                        )
                if not isinstance(body, dict) or not isinstance(body.get("code"), int):
                    raise BilibiliError(
                        f"Invalid Bilibili response from {endpoint}",
                        ambiguous=method == "POST",
                    )
                if body["code"] != 0:
                    message = body.get("message")
                    detail = (
                        message
                        if isinstance(message, str) and message
                        else "Bilibili API request failed"
                    )
                    raise BilibiliError(f"{endpoint}: {detail}", code=body["code"])
                return body.get("data", {})
            except RequestsError as error:
                curl_code = getattr(error, "code", None)
                suffix = f" (curl code {curl_code})" if curl_code is not None else ""
                last_error = BilibiliError(
                    f"Transport error from {endpoint}{suffix}",
                    ambiguous=method == "POST",
                )
            except (json.JSONDecodeError, ValueError, TypeError) as error:
                last_error = BilibiliError(
                    f"Invalid response from {endpoint}: {type(error).__name__}",
                    ambiguous=method == "POST",
                )
            except BilibiliError as error:
                last_error = error
            if attempt < attempts:
                self.logger.warning(
                    "Bilibili GET failed at %s; retrying (%s/%s)",
                    endpoint,
                    attempt,
                    attempts,
                )
                await self.sleep(2 ** (attempt - 1))
        raise last_error or BilibiliError(f"Bilibili request failed at {endpoint}")

    def _signed(self, data: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            **data,
            "access_key": self.access_key,
            "actionKey": "appkey",
            "appkey": APP_KEY,
            "ts": int(self.wall_time()),
        }
        query = urlencode(sorted(payload.items()))
        return {
            **payload,
            "sign": hashlib.md5(f"{query}{APP_SECRET}".encode()).hexdigest(),
        }

    def _headers(self) -> dict[str, str]:
        headers = dict(APP_HEADERS)
        headers["x-bili-mid"] = str(self.mid)
        headers["Cookie"] = f"buvid3={self.buvid3}"
        return headers


def _client_sign(data: Mapping[str, Any]) -> str:
    result = json.dumps(data, separators=(",", ":"))
    for algorithm in ("sha512", "sha3_512", "sha384", "sha3_384", "blake2b"):
        result = hashlib.new(algorithm, result.encode()).hexdigest()
    return result


def _random_token(length: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choices(alphabet, k=length))


def _is_live(value: Any) -> bool:
    return value in (1, "1", True)


def _mapping(value: Any, field: str, *, ambiguous: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BilibiliError(f"{field} must be a mapping", ambiguous=ambiguous)
    return value


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise BilibiliError(f"{field} must be a positive integer") from error
    if result <= 0:
        raise BilibiliError(f"{field} must be a positive integer")
    return result


def _non_negative_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise BilibiliError(f"{field} must be a non-negative integer") from error
    if result < 0:
        raise BilibiliError(f"{field} must be a non-negative integer")
    return result
