import asyncio
import time
from dataclasses import replace
from datetime import date, datetime

import pytest

from bilibili_live_helper.bilibili import BilibiliError, HeartbeatSession
from bilibili_live_helper.config import Settings
from bilibili_live_helper.models import LiveRoom
from bilibili_live_helper.notify import NotificationError
from bilibili_live_helper.runner import (
    SHANGHAI,
    LiveTaskRunner,
    MinimumInterval,
    _watch_summary,
)
from bilibili_live_helper.state import AppState, RoomProgress, StateStore, WatchProgress


NOW = datetime(2026, 7, 11, 12, tzinfo=SHANGHAI)


class FakeClient:
    def __init__(self, rooms=None):
        self.rooms = (
            [LiveRoom(1, 101, "Alpha", "Live", 9, 6)] if rooms is None else rooms
        )
        self.likes = 0
        self.danmakus = 0
        self.heartbeats: list[int] = []

    async def discover_live_rooms(self, _anchor_ids):
        return self.rooms

    async def like(self, _room, _click_count):
        self.likes += 1

    async def send_danmaku(self, _room):
        self.danmakus += 1
        return "[花]"

    def new_heartbeat_session(self, room):
        return HeartbeatSession("uuid", "click", f"session-{room.room_id}")

    async def heartbeat(self, room, _session, _watch_seconds):
        self.heartbeats.append(room.anchor_id)


class FakeNotifier:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0
        self.messages = []

    async def publish(self, title, message, *, tags, sequence_id):
        self.calls += 1
        if self.calls <= self.failures:
            raise NotificationError("temporary failure")
        self.messages.append((title, message, tags, sequence_id))


@pytest.mark.asyncio
async def test_watch_starts_before_slow_like_sequence_finishes(tmp_path):
    like_started = asyncio.Event()
    release_like = asyncio.Event()
    heartbeat_started = asyncio.Event()

    class BlockingClient(FakeClient):
        async def like(self, room, click_count):
            like_started.set()
            await release_like.wait()
            await super().like(room, click_count)

        async def heartbeat(self, room, session, watch_seconds):
            heartbeat_started.set()
            await super().heartbeat(room, session, watch_seconds)

    runner = _runner(
        tmp_path,
        BlockingClient(),
        _settings(
            watch_uids=(1,), watch_minutes=1, like_request_count=1, danmaku_count=0
        ),
    )

    await runner.run_once()
    automation = runner.automation_tasks[1]
    watch = runner.watch_task
    await asyncio.wait_for(heartbeat_started.wait(), timeout=1)

    assert like_started.is_set()
    assert watch is not None and watch.done()
    assert not automation.done()

    release_like.set()
    await automation


@pytest.mark.asyncio
async def test_combined_notification_does_not_wait_for_watch(tmp_path):
    watch_release = asyncio.Event()

    class BlockingWatchClient(FakeClient):
        async def heartbeat(self, room, session, watch_seconds):
            await watch_release.wait()
            await super().heartbeat(room, session, watch_seconds)

    notifier = FakeNotifier()
    runner = _runner(
        tmp_path,
        BlockingWatchClient(),
        _settings(
            watch_uids=(1,), watch_minutes=2, like_request_count=1, danmaku_count=1
        ),
        notifier=notifier,
    )

    await runner.run_once()
    automation = runner.automation_tasks[1]
    watch = runner.watch_task
    await automation

    assert watch is not None and not watch.done()
    assert "bilibili-2026-07-11-1-automation-complete" in runner.state.outbox
    await runner._stop_watch("pending")


@pytest.mark.asyncio
async def test_restart_resumes_only_remaining_batches(tmp_path):
    settings = _settings(like_request_count=2, danmaku_count=2)
    store = StateStore(tmp_path / "state.json")
    store.save(
        AppState(
            day=NOW.date(),
            rooms={
                1: RoomProgress(
                    1,
                    101,
                    "Alpha",
                    likes_sent=1,
                    like_attempts=1,
                    danmaku_sent=1,
                    danmaku_attempts=1,
                    updated_at=1,
                )
            },
        )
    )
    client = FakeClient()
    runner = LiveTaskRunner(
        client,
        settings,
        store,
        notifier=FakeNotifier(),
        sleep=_no_sleep,
        now=lambda: NOW,
        wall_time=lambda: 100,
    )

    await runner.run_once()
    await runner.automation_tasks[1]

    assert client.likes == 1
    assert client.danmakus == 1
    assert runner.state.rooms[1].likes_sent == 2
    assert runner.state.rooms[1].danmaku_sent == 2
    assert runner.state.rooms[1].notification_queued


@pytest.mark.asyncio
async def test_watch_priority_uses_configured_order(tmp_path):
    rooms = [LiveRoom(1, 101, "Alpha", "", 9, 6), LiveRoom(2, 202, "Beta", "", 9, 6)]
    client = FakeClient(rooms)
    settings = _settings(
        include_uids=(1, 2), watch_uids=(2, 1), watch_minutes=1, danmaku_count=0
    )
    store = StateStore(tmp_path / "state.json")
    store.save(
        AppState(
            day=NOW.date(),
            rooms={
                1: RoomProgress(
                    1,
                    101,
                    "Alpha",
                    likes_sent=1,
                    like_attempts=1,
                    notification_queued=True,
                ),
                2: RoomProgress(
                    2,
                    202,
                    "Beta",
                    likes_sent=1,
                    like_attempts=1,
                    notification_queued=True,
                ),
            },
        )
    )
    runner = LiveTaskRunner(client, settings, store, sleep=_no_sleep, now=lambda: NOW)

    await runner.run_once()
    watch = runner.watch_task
    assert watch is not None
    await watch

    assert client.heartbeats[0] == 2


@pytest.mark.asyncio
async def test_offline_poll_stops_active_watch(tmp_path):
    watch_sleep_started = asyncio.Event()

    async def blocking_sleep(seconds):
        if seconds > 0:
            watch_sleep_started.set()
            await asyncio.Event().wait()

    client = FakeClient()
    settings = _settings(
        watch_uids=(1,), watch_minutes=2, like_request_count=1, danmaku_count=0
    )
    runner = _runner(tmp_path, client, settings, sleep=blocking_sleep)

    await runner.run_once()
    await asyncio.wait_for(watch_sleep_started.wait(), timeout=1)
    client.rooms = []
    await runner.run_once()

    assert runner.watch_task is None
    assert runner.state.watches[1].status == "stream_ended"
    assert not client.heartbeats


@pytest.mark.asyncio
async def test_rollover_keeps_outbox_and_queues_watch_summary(tmp_path):
    notifier = FakeNotifier()
    store = StateStore(tmp_path / "state.json")
    store.save(
        AppState(
            day=NOW.date(),
            rooms={1: RoomProgress(1, 101, "Alpha", likes_sent=1, like_attempts=1)},
            watches={
                1: WatchProgress(
                    1,
                    101,
                    "Alpha",
                    heartbeat_count=7,
                    watched_seconds=420,
                    watch_seconds_attempted=420,
                    status="pending",
                    updated_at=1,
                )
            },
        )
    )
    runner = LiveTaskRunner(
        FakeClient([]),
        _settings(watch_minutes=150),
        store,
        notifier=notifier,
        now=lambda: NOW,
    )

    await runner._rollover(date(2026, 7, 12))

    assert runner.state.day == date(2026, 7, 12)
    assert not runner.state.rooms
    assert not runner.state.watches
    summary = runner.state.outbox["bilibili-watch-2026-07-11"]
    assert "Alpha (UID: 1): 7/150 confirmed minutes, day ended" in summary.message


@pytest.mark.asyncio
async def test_rollover_cancels_old_day_work_before_reset(tmp_path):
    like_started = asyncio.Event()

    class BlockingClient(FakeClient):
        async def like(self, _room, _click_count):
            like_started.set()
            await asyncio.Event().wait()

    runner = _runner(
        tmp_path,
        BlockingClient(),
        _settings(like_request_count=1, danmaku_count=0),
    )
    await runner.run_once()
    old_task = runner.automation_tasks[1]
    await asyncio.wait_for(like_started.wait(), timeout=1)

    await runner._rollover(date(2026, 7, 12))

    assert old_task.cancelled()
    assert runner.state.day == date(2026, 7, 12)
    assert not runner.state.rooms


@pytest.mark.asyncio
async def test_refresh_discards_a_snapshot_that_crosses_midnight(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    clock = [NOW]

    class SlowRefreshClient(FakeClient):
        async def discover_live_rooms(self, anchor_ids):
            started.set()
            await release.wait()
            return await super().discover_live_rooms(anchor_ids)

    runner = LiveTaskRunner(
        SlowRefreshClient(),
        _settings(like_request_count=1, danmaku_count=0),
        StateStore(tmp_path / "state.json"),
        sleep=_no_sleep,
        now=lambda: clock[0],
        wall_time=lambda: 100,
    )
    refresh = asyncio.create_task(runner.run_once())

    await asyncio.wait_for(started.wait(), timeout=1)
    clock[0] = datetime(2026, 7, 12, 0, tzinfo=SHANGHAI)
    await runner._rollover(clock[0].date())
    release.set()
    await refresh

    assert runner.state.day == date(2026, 7, 12)
    assert not runner.live_rooms
    assert not runner.automation_tasks


@pytest.mark.asyncio
async def test_danmaku_rechecks_liveness_after_waiting_for_global_gate(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_sleep(seconds):
        if seconds > 0:
            started.set()
            await release.wait()

    client = FakeClient()
    runner = _runner(
        tmp_path,
        client,
        _settings(like_request_count=1, danmaku_count=1),
        sleep=blocking_sleep,
    )
    runner.danmaku_gate.next_available = time.monotonic() + 60

    await runner.run_once()
    automation = runner.automation_tasks[1]
    await asyncio.wait_for(started.wait(), timeout=1)
    client.rooms = []
    await runner.run_once()
    release.set()
    await automation

    assert client.danmakus == 0


@pytest.mark.asyncio
async def test_cancelled_global_gate_does_not_reserve_a_future_slot():
    started = asyncio.Event()

    async def blocking_sleep(seconds):
        assert seconds > 0
        started.set()
        await asyncio.Event().wait()

    gate = MinimumInterval(3, blocking_sleep)
    next_available = time.monotonic() + 60
    gate.next_available = next_available
    waiter = asyncio.create_task(gate.wait())

    await asyncio.wait_for(started.wait(), timeout=1)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)

    assert gate.next_available == next_available


def test_running_watch_is_recovered_as_pending(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(
        AppState(
            day=NOW.date(),
            watches={1: WatchProgress(1, 101, "Alpha", status="running", updated_at=1)},
        )
    )

    runner = LiveTaskRunner(FakeClient([]), _settings(), store, now=lambda: NOW)

    assert runner.state.watches[1].status == "pending"
    assert store.load(NOW.date()).watches[1].status == "pending"


@pytest.mark.asyncio
async def test_notification_outbox_retries_and_removes_only_after_success(tmp_path):
    clock = [100.0]
    notifier = FakeNotifier(failures=1)
    runner = _runner(
        tmp_path,
        FakeClient([]),
        _settings(),
        notifier=notifier,
        wall_time=lambda: clock[0],
    )
    runner._queue_notification(
        sequence_id="bilibili-test",
        title="Test",
        message="Body",
        tags="test_tube",
    )

    assert runner.outbox is not None
    await runner.outbox.flush_once()
    pending = runner.state.outbox["bilibili-test"]
    assert pending.attempts == 1
    assert pending.next_attempt_at == 130

    await runner.outbox.flush_once()
    assert notifier.calls == 1

    clock[0] = 130
    await runner.outbox.flush_once()
    assert "bilibili-test" not in runner.state.outbox
    assert notifier.messages[0][3] == "bilibili-test"


@pytest.mark.asyncio
async def test_watch_minutes_are_seconds_based_for_non_default_interval(tmp_path):
    durations: list[int] = []

    class DurationClient(FakeClient):
        async def heartbeat(self, room, session, watch_seconds):
            durations.append(watch_seconds)
            await super().heartbeat(room, session, watch_seconds)

    runner = _runner(
        tmp_path,
        DurationClient(),
        _settings(
            watch_uids=(1,),
            watch_minutes=2,
            heartbeat_interval_seconds=90,
            like_request_count=1,
            danmaku_count=0,
        ),
    )

    await runner.run_once()
    watch = runner.watch_task
    assert watch is not None
    await watch

    progress = runner.state.watches[1]
    assert durations == [90, 30]
    assert progress.watched_seconds == 120
    assert progress.watch_seconds_attempted == 120
    assert progress.status == "completed"


@pytest.mark.asyncio
async def test_live_rooms_do_not_hold_like_lock_while_waiting_between_batches(tmp_path):
    both_rooms_started = asyncio.Event()
    release_interval = asyncio.Event()
    first_rooms: set[int] = set()

    class ConcurrentClient(FakeClient):
        async def like(self, room, click_count):
            first_rooms.add(room.anchor_id)
            if len(first_rooms) == 2:
                both_rooms_started.set()
            await super().like(room, click_count)

    async def blocking_interval(seconds):
        if seconds > 0:
            await release_interval.wait()

    rooms = [
        LiveRoom(1, 101, "Alpha", "", 9, 6),
        LiveRoom(2, 202, "Beta", "", 9, 6),
    ]
    runner = _runner(
        tmp_path,
        ConcurrentClient(rooms),
        _settings(
            include_uids=(1, 2),
            like_request_count=2,
            danmaku_count=0,
        ),
        sleep=blocking_interval,
    )

    await runner.run_once()
    tasks = list(runner.automation_tasks.values())
    await asyncio.wait_for(both_rooms_started.wait(), timeout=1)
    release_interval.set()
    await asyncio.gather(*tasks)

    assert first_rooms == {1, 2}


@pytest.mark.asyncio
async def test_failed_live_refresh_cancels_tasks_using_stale_status(tmp_path):
    like_started = asyncio.Event()

    class FailingRefreshClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.fail_refresh = False

        async def discover_live_rooms(self, anchor_ids):
            if self.fail_refresh:
                raise BilibiliError("refresh failed")
            return await super().discover_live_rooms(anchor_ids)

        async def like(self, room, click_count):
            like_started.set()
            await asyncio.Event().wait()

    client = FailingRefreshClient()
    runner = _runner(
        tmp_path,
        client,
        _settings(like_request_count=1, danmaku_count=0),
    )

    await runner.run_once()
    await asyncio.wait_for(like_started.wait(), timeout=1)
    client.fail_refresh = True

    with pytest.raises(BilibiliError, match="refresh failed"):
        await runner.run_once()

    assert not runner.live_rooms
    assert not runner.automation_tasks


@pytest.mark.asyncio
async def test_ambiguous_like_is_reserved_and_not_repeated(tmp_path):
    class AmbiguousClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def like(self, room, click_count):
            self.calls += 1
            raise BilibiliError("transport failed", ambiguous=True)

    client = AmbiguousClient()
    notifier = FakeNotifier()
    runner = _runner(
        tmp_path,
        client,
        _settings(like_request_count=1, danmaku_count=0),
        notifier=notifier,
    )

    await runner.run_once()
    await runner.automation_tasks[1]
    await asyncio.sleep(0)
    await runner.run_once()

    progress = runner.state.rooms[1]
    assert client.calls == 1
    assert progress.like_attempts == 1
    assert progress.likes_sent == 0
    assert "bilibili-2026-07-11-1-like-error" in runner.state.outbox
    completion = runner.state.outbox["bilibili-2026-07-11-1-automation-complete"]
    assert "outcome uncertain" in completion.title


@pytest.mark.asyncio
async def test_definitive_like_rejection_is_retried_on_next_poll(tmp_path):
    class RetryClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def like(self, room, click_count):
            self.calls += 1
            if self.calls == 1:
                raise BilibiliError("denied", code=10030)
            await super().like(room, click_count)

    client = RetryClient()
    runner = _runner(
        tmp_path,
        client,
        _settings(like_request_count=1, danmaku_count=0),
    )

    await runner.run_once()
    await runner.automation_tasks[1]
    await asyncio.sleep(0)
    assert runner.state.rooms[1].like_attempts == 0

    await runner.run_once()
    await runner.automation_tasks[1]

    assert client.calls == 2
    assert runner.state.rooms[1].like_attempts == 1
    assert runner.state.rooms[1].likes_sent == 1


@pytest.mark.asyncio
async def test_background_worker_failure_stops_runner(tmp_path):
    class BrokenRunner(LiveTaskRunner):
        async def _midnight_loop(self):
            raise RuntimeError("worker failed")

    runner = BrokenRunner(
        FakeClient([]),
        _settings(),
        StateStore(tmp_path / "state.json"),
        sleep=asyncio.sleep,
        now=lambda: NOW,
    )

    with pytest.raises(ExceptionGroup) as captured:
        await runner.run_forever(asyncio.Event())

    assert any(isinstance(error, RuntimeError) for error in captured.value.exceptions)


@pytest.mark.asyncio
async def test_unexpected_automation_failure_stops_runner(tmp_path):
    class BrokenActionClient(FakeClient):
        async def like(self, room, click_count):
            raise RuntimeError("programming error")

    runner = LiveTaskRunner(
        BrokenActionClient(),
        _settings(like_request_count=1, danmaku_count=0),
        StateStore(tmp_path / "state.json"),
        sleep=asyncio.sleep,
        now=lambda: NOW,
    )

    with pytest.raises(ExceptionGroup) as captured:
        await runner.run_forever(asyncio.Event())

    assert any(isinstance(error, ExceptionGroup) for error in captured.value.exceptions)


@pytest.mark.asyncio
async def test_stop_request_cancels_blocked_live_refresh(tmp_path):
    refresh_started = asyncio.Event()

    class BlockingRefreshClient(FakeClient):
        async def discover_live_rooms(self, anchor_ids):
            refresh_started.set()
            await asyncio.Event().wait()

    runner = LiveTaskRunner(
        BlockingRefreshClient(),
        _settings(),
        StateStore(tmp_path / "state.json"),
        sleep=asyncio.sleep,
        now=lambda: NOW,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop_event))

    await asyncio.wait_for(refresh_started.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert runner.stopping


def test_watch_summary_reports_empty_day_without_fake_completion():
    assert _watch_summary([], NOW.date(), 150) == "Date: 2026-07-11\nAttempted: 0"


def _settings(**changes) -> Settings:
    defaults = Settings(
        include_uids=(1,),
        watch_uids=(),
        poll_interval_seconds=120,
        request_timeout_seconds=15,
        api_interval_seconds=1,
        like_clicks_per_request=30,
        like_request_count=1,
        like_interval_seconds=1,
        watch_minutes=0,
        heartbeat_interval_seconds=60,
        danmaku_count=1,
        danmaku_interval_seconds=1,
        global_danmaku_interval_seconds=1,
        ntfy=None,
    )
    return replace(defaults, **changes)


def _runner(
    tmp_path, client, settings, *, notifier=None, sleep=None, wall_time=lambda: 100
):
    return LiveTaskRunner(
        client,
        settings,
        StateStore(tmp_path / "state.json"),
        notifier=notifier,
        sleep=sleep or _no_sleep,
        now=lambda: NOW,
        wall_time=wall_time,
    )


async def _no_sleep(_seconds):
    return None
