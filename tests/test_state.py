import json
import stat
from datetime import date

from bilibili_live_helper.state import (
    AppState,
    OutboxMessage,
    RoomProgress,
    StateStore,
    WatchProgress,
)


def test_state_round_trip_preserves_phase_progress_and_outbox(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = AppState(
        day=date(2026, 7, 11),
        rooms={
            1: RoomProgress(
                1,
                101,
                "Alpha",
                likes_sent=4,
                like_attempts=4,
                danmaku_sent=2,
                danmaku_attempts=2,
                updated_at=10,
            )
        },
        watches={
            1: WatchProgress(
                1,
                101,
                "Alpha",
                heartbeat_count=7,
                watched_seconds=420,
                watch_seconds_attempted=420,
                status="pending",
                updated_at=11,
            )
        },
        outbox={
            "bilibili-test": OutboxMessage(
                "bilibili-test", "Title", "Body", "eyes", attempts=2
            )
        },
        last_successful_poll_at=12,
    )

    store.save(state)
    loaded = store.load(date(2026, 7, 12))

    assert loaded.day == date(2026, 7, 11)
    assert loaded.rooms[1].likes_sent == 4
    assert loaded.rooms[1].like_attempts == 4
    assert loaded.rooms[1].danmaku_sent == 2
    assert loaded.rooms[1].danmaku_attempts == 2
    assert loaded.watches[1].heartbeat_count == 7
    assert loaded.watches[1].watched_seconds == 420
    assert loaded.watches[1].watch_seconds_attempted == 420
    assert loaded.outbox["bilibili-test"].attempts == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invalid_state_is_quarantined_instead_of_blocking_startup(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 0}), encoding="utf-8")
    store = StateStore(path)

    state = store.load(date(2026, 7, 11))

    assert state.day == date(2026, 7, 11)
    assert not path.exists()
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1


def test_previous_state_schema_is_quarantined(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "day": "2026-07-11",
                "rooms": {
                    "1": {
                        "anchor_id": 1,
                        "room_id": 101,
                        "anchor_name": "Alpha",
                        "likes_sent": 4,
                        "danmaku_sent": 2,
                        "notification_queued": False,
                        "last_error": None,
                        "updated_at": 10,
                    }
                },
                "watches": {
                    "1": {
                        "anchor_id": 1,
                        "room_id": 101,
                        "anchor_name": "Alpha",
                        "heartbeat_count": 7,
                        "status": "pending",
                        "last_error": None,
                        "updated_at": 11,
                    }
                },
                "outbox": {},
                "last_successful_poll_at": 12,
            }
        ),
        encoding="utf-8",
    )

    state = StateStore(path).load(date(2026, 7, 11))

    assert state.day == date(2026, 7, 11)
    assert not path.exists()
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1
