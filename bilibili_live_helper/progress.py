"""Strict task state, shared by execution and persistent snapshots."""

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

Uid = Annotated[int, Field(gt=0)]
Count = Annotated[int, Field(ge=0)]
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class StateModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    @model_validator(mode="before")
    @classmethod
    def complete_snapshot(cls, value, info: ValidationInfo):
        if info.mode == "json" and isinstance(value, dict):
            missing = cls.model_fields.keys() - value.keys()
            if missing:
                raise ValueError(
                    f"Incomplete {cls.__name__}: {', '.join(sorted(missing))}"
                )
        return value


class RoomProgress(StateModel):
    anchor_id: Uid
    room_id: Uid
    anchor_name: str
    likes_sent: Count = 0
    like_attempts: Count = 0
    danmaku_sent: Count = 0
    danmaku_attempts: Count = 0
    notification_queued: bool = False
    last_error: str | None = None
    updated_at: Timestamp = 0.0

    @model_validator(mode="after")
    def confirmed_attempts(self):
        if self.like_attempts < self.likes_sent:
            raise ValueError("like_attempts cannot be less than likes_sent")
        if self.danmaku_attempts < self.danmaku_sent:
            raise ValueError("danmaku_attempts cannot be less than danmaku_sent")
        return self


class WatchProgress(StateModel):
    anchor_id: Uid
    room_id: Uid
    anchor_name: str
    heartbeat_count: Count = 0
    watched_seconds: Count = 0
    watch_seconds_attempted: Count = 0
    status: Literal[
        "pending", "running", "completed", "uncertain", "stream_ended", "day_ended"
    ] = "pending"
    last_error: str | None = None
    updated_at: Timestamp = 0.0

    @model_validator(mode="after")
    def confirmed_attempts(self):
        if self.watch_seconds_attempted < self.watched_seconds:
            raise ValueError(
                "watch_seconds_attempted cannot be less than watched_seconds"
            )
        return self


class OutboxMessage(StateModel):
    sequence_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
    title: str
    message: str
    tags: str
    attempts: Count = 0
    next_attempt_at: Timestamp = 0.0


class AppState(StateModel):
    version: Literal[3] = 3
    day: date
    rooms: dict[Uid, RoomProgress] = Field(default_factory=dict)
    watches: dict[Uid, WatchProgress] = Field(default_factory=dict)
    outbox: dict[str, OutboxMessage] = Field(default_factory=dict)
    last_successful_poll_at: Timestamp | None = None
    poll_failures: Count = 0
    poll_failed_at: Timestamp | None = None

    @field_validator("day", mode="before")
    @classmethod
    def json_date(cls, value, info: ValidationInfo):
        if info.mode == "json" and isinstance(value, str):
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError("State day must use YYYY-MM-DD")
            return parsed
        return value

    @field_validator("rooms", "watches", mode="before")
    @classmethod
    def json_uid_keys(cls, value, info: ValidationInfo):
        if info.mode == "json" and isinstance(value, dict):
            if any(
                not key.isascii() or not key.isdigit() or str(int(key)) != key
                for key in value
            ):
                raise ValueError("State UID keys must be canonical positive integers")
            return {int(key): item for key, item in value.items()}
        return value

    @model_validator(mode="after")
    def identities(self):
        for collection in (self.rooms, self.watches):
            if any(uid != item.anchor_id for uid, item in collection.items()):
                raise ValueError("Progress key does not match anchor_id")
        if any(key != item.sequence_id for key, item in self.outbox.items()):
            raise ValueError("Outbox key does not match sequence_id")
        return self
