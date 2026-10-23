import json
import stat
from datetime import date

import pytest

from bilibili_live_helper.progress import (
    AppState,
    OutboxMessage,
    RoomProgress,
    WatchProgress,
)
from bilibili_live_helper.state import StateStore


def test_state_round_trip_preserves_phase_progress_and_outbox(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = AppState(
        day=date(2026, 7, 11),
        rooms={
            1: RoomProgress(
                anchor_id=1,
                room_id=101,
                anchor_name="Alpha",
                likes_sent=4,
                like_attempts=4,
                danmaku_sent=2,
                danmaku_attempts=2,
                updated_at=10,
            )
        },
        watches={
            1: WatchProgress(
                anchor_id=1,
                room_id=101,
                anchor_name="Alpha",
                heartbeat_count=7,
                watched_seconds=420,
                watch_seconds_attempted=420,
                status="pending",
                updated_at=11,
            )
        },
        outbox={
            "bilibili-test": OutboxMessage(
                sequence_id="bilibili-test",
                title="Title",
                message="Body",
                tags="eyes",
                attempts=2,
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


def test_invalid_state_stops_without_modifying_the_file(tmp_path):
    path = tmp_path / "state.json"
    original = '{"version": 0}'
    path.write_text(original)
    with pytest.raises(ValueError):
        StateStore(path).load(date(2026, 7, 11))
    assert path.read_text() == original


@pytest.mark.parametrize(
    "change",
    [
        lambda state: state["rooms"]["1"].pop("like_attempts"),
        lambda state: state["rooms"]["1"].update(likes_sent=True),
        lambda state: state["rooms"].update({"01": state["rooms"].pop("1")}),
        lambda state: state.update(last_successful_poll_at=float("inf")),
        lambda state: state.update(unexpected=1),
    ],
)
def test_rejects_invalid_persisted_progress(tmp_path, change):
    path = tmp_path / "state.json"
    StateStore(path).save(
        AppState(
            day=date(2026, 7, 11),
            rooms={1: RoomProgress(anchor_id=1, room_id=101, anchor_name="Alpha")},
        )
    )
    payload = json.loads(path.read_text())
    change(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        StateStore(path).load(date(2026, 7, 11))


def test_save_validates_mutated_progress_before_replacing_file(tmp_path):
    path = tmp_path / "state.json"
    state = AppState(
        day=date(2026, 7, 11),
        rooms={1: RoomProgress(anchor_id=1, room_id=101, anchor_name="Alpha")},
    )
    store = StateStore(path)
    store.save(state)
    original = path.read_bytes()
    state.rooms[1].likes_sent = 3
    with pytest.raises(ValueError):
        store.save(state)
    assert path.read_bytes() == original
