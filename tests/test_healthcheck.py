import json
from datetime import date

import pytest

from bilibili_live_helper.healthcheck import check_health
from bilibili_live_helper.progress import AppState
from bilibili_live_helper.state import StateStore


def test_healthcheck_accepts_recent_successful_poll(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("include_uids: [1]\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    StateStore(state_path).save(
        AppState(day=date(2026, 7, 11), last_successful_poll_at=1_000),
    )

    assert check_health(config, state_path, now=1_100) == (True, "ok")


def test_healthcheck_rejects_stale_or_missing_state(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("include_uids: [1]\n", encoding="utf-8")
    state_path = tmp_path / "state.json"

    healthy, message = check_health(config, state_path, now=1_000)
    assert not healthy
    assert "unreadable" in message

    StateStore(state_path).save(
        AppState(day=date(2026, 7, 11), last_successful_poll_at=1_000),
    )
    healthy, message = check_health(config, state_path, now=1_361)
    assert not healthy
    assert "361 seconds old" in message


@pytest.mark.parametrize("timestamp", [float("nan"), 10**400])
def test_healthcheck_rejects_invalid_poll_timestamp(tmp_path, timestamp):
    config = tmp_path / "config.yaml"
    config.write_text("include_uids: [1]\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "day": "2026-07-11",
                "rooms": {},
                "watches": {},
                "outbox": {},
                "last_successful_poll_at": timestamp,
            }
        ),
        encoding="utf-8",
    )

    healthy, message = check_health(config, state_path, now=1_000)

    assert not healthy
    assert "unreadable" in message
