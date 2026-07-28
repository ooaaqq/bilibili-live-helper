import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

STATE_VERSION = 2
WATCH_STATUSES = {
    "pending",
    "running",
    "completed",
    "uncertain",
    "stream_ended",
    "day_ended",
}


@dataclass
class RoomProgress:
    anchor_id: int
    room_id: int
    anchor_name: str
    likes_sent: int = 0
    like_attempts: int = 0
    danmaku_sent: int = 0
    danmaku_attempts: int = 0
    notification_queued: bool = False
    last_error: str | None = None
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.likes_sent < 0 or self.danmaku_sent < 0:
            raise ValueError("Confirmed room progress cannot be negative")
        if self.like_attempts < self.likes_sent:
            raise ValueError("like_attempts cannot be less than likes_sent")
        if self.danmaku_attempts < self.danmaku_sent:
            raise ValueError("danmaku_attempts cannot be less than danmaku_sent")


@dataclass
class WatchProgress:
    anchor_id: int
    room_id: int
    anchor_name: str
    heartbeat_count: int = 0
    watched_seconds: int = 0
    watch_seconds_attempted: int = 0
    status: str = "pending"
    last_error: str | None = None
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.heartbeat_count < 0:
            raise ValueError("heartbeat_count cannot be negative")
        if self.watched_seconds < 0:
            raise ValueError("watched_seconds cannot be negative")
        if self.watch_seconds_attempted < self.watched_seconds:
            raise ValueError(
                "watch_seconds_attempted cannot be less than watched_seconds"
            )


@dataclass
class OutboxMessage:
    sequence_id: str
    title: str
    message: str
    tags: str
    attempts: int = 0
    next_attempt_at: float = 0.0


@dataclass
class AppState:
    day: date
    rooms: dict[int, RoomProgress] = field(default_factory=dict)
    watches: dict[int, WatchProgress] = field(default_factory=dict)
    outbox: dict[str, OutboxMessage] = field(default_factory=dict)
    last_successful_poll_at: float | None = None


class StateStore:
    def __init__(self, path: Path, logger: logging.Logger | None = None):
        self.path = path
        self.logger = logger or logging.getLogger(__name__)

    def load(self, default_day: date) -> AppState:
        try:
            return read_state(self.path)
        except FileNotFoundError:
            return AppState(day=default_day)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            quarantine = self._quarantine()
            self.logger.warning("Invalid state file moved to %s: %s", quarantine, error)
            return AppState(day=default_day)
        except OSError as error:
            raise RuntimeError(f"Unable to read state file: {error}") from error

    def save(self, state: AppState) -> None:
        payload = {
            "version": STATE_VERSION,
            "day": state.day.isoformat(),
            "rooms": {
                str(uid): asdict(progress)
                for uid, progress in sorted(state.rooms.items())
            },
            "watches": {
                str(uid): asdict(progress)
                for uid, progress in sorted(state.watches.items())
            },
            "outbox": {
                sequence_id: asdict(message)
                for sequence_id, message in sorted(state.outbox.items())
            },
            "last_successful_poll_at": state.last_successful_poll_at,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary_path.write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(self.path)
        except OSError as error:
            raise RuntimeError(f"Unable to save state file: {error}") from error

    def _quarantine(self) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        quarantine = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
        try:
            self.path.replace(quarantine)
        except OSError as error:
            raise RuntimeError(
                f"Unable to quarantine invalid state file: {error}"
            ) from error
        return quarantine


def read_state(path: Path) -> AppState:
    return _parse_state(json.loads(path.read_text(encoding="utf-8")))


def _parse_state(value: Any) -> AppState:
    root = _mapping(value, "state")
    if root.get("version") != STATE_VERSION:
        raise ValueError(f"Unsupported state version: {root.get('version')}")
    state = AppState(day=date.fromisoformat(_string(root, "day")))
    state.rooms = {}
    for uid_value, item in _mapping(root.get("rooms"), "rooms").items():
        uid = _uid_key(uid_value)
        progress = _room_progress(_mapping(item, f"rooms.{uid_value}"))
        if progress.anchor_id != uid:
            raise ValueError(f"Room key does not match anchor_id: {uid}")
        state.rooms[uid] = progress
    state.watches = {}
    for uid_value, item in _mapping(root.get("watches"), "watches").items():
        uid = _uid_key(uid_value)
        progress = _watch_progress(_mapping(item, f"watches.{uid_value}"))
        if progress.anchor_id != uid:
            raise ValueError(f"Watch key does not match anchor_id: {uid}")
        state.watches[uid] = progress
    state.outbox = {
        sequence_id: _outbox_message(
            _mapping(item, f"outbox.{sequence_id}"), sequence_id
        )
        for sequence_id, item in _mapping(root.get("outbox"), "outbox").items()
    }
    last_poll = root.get("last_successful_poll_at")
    if last_poll is not None and not _number(last_poll):
        raise ValueError("last_successful_poll_at must be a number or null")
    state.last_successful_poll_at = float(last_poll) if last_poll is not None else None
    return state


def _room_progress(value: dict[str, Any]) -> RoomProgress:
    likes_sent = _non_negative_int(value, "likes_sent")
    like_attempts = _non_negative_int(value, "like_attempts")
    danmaku_sent = _non_negative_int(value, "danmaku_sent")
    danmaku_attempts = _non_negative_int(value, "danmaku_attempts")
    if like_attempts < likes_sent:
        raise ValueError("like_attempts cannot be less than likes_sent")
    if danmaku_attempts < danmaku_sent:
        raise ValueError("danmaku_attempts cannot be less than danmaku_sent")
    return RoomProgress(
        anchor_id=_positive_int(value, "anchor_id"),
        room_id=_positive_int(value, "room_id"),
        anchor_name=_string(value, "anchor_name"),
        likes_sent=likes_sent,
        like_attempts=like_attempts,
        danmaku_sent=danmaku_sent,
        danmaku_attempts=danmaku_attempts,
        notification_queued=_boolean(value, "notification_queued"),
        last_error=_optional_string(value, "last_error"),
        updated_at=_float(value, "updated_at"),
    )


def _watch_progress(value: dict[str, Any]) -> WatchProgress:
    status = _string(value, "status")
    if status not in WATCH_STATUSES:
        raise ValueError(f"Invalid watch status: {status}")
    heartbeat_count = _non_negative_int(value, "heartbeat_count")
    watched_seconds = _non_negative_int(value, "watched_seconds")
    watch_seconds_attempted = _non_negative_int(value, "watch_seconds_attempted")
    if watch_seconds_attempted < watched_seconds:
        raise ValueError("watch_seconds_attempted cannot be less than watched_seconds")
    return WatchProgress(
        anchor_id=_positive_int(value, "anchor_id"),
        room_id=_positive_int(value, "room_id"),
        anchor_name=_string(value, "anchor_name"),
        heartbeat_count=heartbeat_count,
        watched_seconds=watched_seconds,
        watch_seconds_attempted=watch_seconds_attempted,
        status=status,
        last_error=_optional_string(value, "last_error"),
        updated_at=_float(value, "updated_at"),
    )


def _outbox_message(value: dict[str, Any], sequence_id: str) -> OutboxMessage:
    if _string(value, "sequence_id") != sequence_id:
        raise ValueError(f"Outbox key does not match sequence_id: {sequence_id}")
    return OutboxMessage(
        sequence_id=sequence_id,
        title=_string(value, "title"),
        message=_string(value, "message"),
        tags=_string(value, "tags"),
        attempts=_non_negative_int(value, "attempts"),
        next_attempt_at=_float(value, "next_attempt_at"),
    )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a string-keyed mapping")
    return value


def _uid_key(value: str) -> int:
    try:
        uid = int(value)
    except ValueError as error:
        raise ValueError(f"Invalid UID key: {value}") from error
    if uid <= 0 or str(uid) != value:
        raise ValueError(f"Invalid UID key: {value}")
    return uid


def _string(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str):
        raise TypeError(f"{field} must be a string")
    return item


def _optional_string(value: dict[str, Any], field: str) -> str | None:
    item = value.get(field)
    if item is not None and not isinstance(item, str):
        raise ValueError(f"{field} must be a string or null")
    return item


def _positive_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return item


def _non_negative_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return item


def _boolean(value: dict[str, Any], field: str) -> bool:
    item = value.get(field)
    if not isinstance(item, bool):
        raise TypeError(f"{field} must be a boolean")
    return item


def _float(value: dict[str, Any], field: str) -> float:
    item = value.get(field)
    if not _number(item):
        raise ValueError(f"{field} must be a number")
    return float(item)


def _number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False
