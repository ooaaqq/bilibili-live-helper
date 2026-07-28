import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime, time as clock_time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .bilibili import BilibiliError, HeartbeatSession
from .config import Settings
from .models import LiveRoom
from .notify import NotificationPublisher, PersistentOutbox
from .state import AppState, OutboxMessage, RoomProgress, StateStore, WatchProgress


SHANGHAI = ZoneInfo("Asia/Shanghai")
Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]
Clock = Callable[[], float]


class _StopRequested(Exception):
    pass


class LiveClient(Protocol):
    async def discover_live_rooms(
        self, anchor_ids: tuple[int, ...]
    ) -> list[LiveRoom]: ...
    async def like(self, room: LiveRoom, click_count: int) -> None: ...
    async def send_danmaku(self, room: LiveRoom) -> str: ...
    def new_heartbeat_session(self, room: LiveRoom) -> HeartbeatSession: ...
    async def heartbeat(
        self, room: LiveRoom, session: HeartbeatSession, watch_seconds: int
    ) -> None: ...


class LiveTaskRunner:
    def __init__(
        self,
        client: LiveClient,
        settings: Settings,
        state_store: StateStore,
        *,
        notifier: NotificationPublisher | None = None,
        sleep: Sleep = asyncio.sleep,
        now: Now | None = None,
        wall_time: Clock = time.time,
        logger: logging.Logger | None = None,
    ):
        self.client = client
        self.settings = settings
        self.state_store = state_store
        self.notifier = notifier
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.wall_time = wall_time
        self.logger = logger or logging.getLogger(__name__)
        self.state = state_store.load(self.now().date())
        recovered_watch = False
        for progress in self.state.watches.values():
            if progress.status == "running":
                progress.status = "pending"
                recovered_watch = True
        if recovered_watch:
            self.state_store.save(self.state)
        self.live_rooms: dict[int, LiveRoom] = {}
        self.automation_tasks: dict[int, asyncio.Task[None]] = {}
        self.watch_task: asyncio.Task[None] | None = None
        self.watch_uid: int | None = None
        self.danmaku_gate = MinimumInterval(
            settings.global_danmaku_interval_seconds, sleep
        )
        self.rollover_lock = asyncio.Lock()
        self.wake_event = asyncio.Event()
        self.outbox = (
            PersistentOutbox(
                notifier,
                state_store,
                lambda: self.state,
                wall_time=wall_time,
                logger=self.logger,
            )
            if notifier
            else None
        )
        self.fatal_error: asyncio.Future[None] | None = None
        self.stopping = False

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        try:
            try:
                self.fatal_error = asyncio.get_running_loop().create_future()
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(
                        self._poll_loop(stop_event), name="live-state-poller"
                    )
                    if self.outbox:
                        tasks.create_task(self.outbox.run(), name="notification-outbox")
                    tasks.create_task(self._midnight_loop(), name="midnight-rollover")
                    tasks.create_task(
                        self._stop_on_request(stop_event), name="stop-request"
                    )
                    tasks.create_task(
                        self._raise_background_failure(), name="background-failure"
                    )
            except* _StopRequested:
                pass
        finally:
            self.stopping = True
            await self.shutdown()
            self.fatal_error = None

    async def run_once(self) -> None:
        try:
            await self._refresh_once()
        except asyncio.CancelledError:
            raise
        except BilibiliError:
            await self._suspend_live_tasks()
            raise

    async def _refresh_once(self) -> None:
        refresh_day = self.now().date()
        await self._ensure_day(refresh_day)
        previous_live = set(self.live_rooms)
        rooms = await self.client.discover_live_rooms(self.settings.include_uids)
        await self._ensure_day(self.now().date())
        if self.state.day != refresh_day:
            self.wake_event.set()
            return
        self.live_rooms = {room.anchor_id: room for room in rooms}
        self.state.last_successful_poll_at = self.wall_time()
        self.state_store.save(self.state)

        for room in rooms:
            if room.anchor_id not in previous_live:
                self.logger.info("Detected %s live", _streamer(room))
            progress = self._room_progress(room)
            if self._automation_complete(progress):
                self._queue_completion(progress)
            elif room.anchor_id not in self.automation_tasks:
                task = asyncio.create_task(
                    self._run_automation(room, self.state.day),
                    name=f"automation-{room.anchor_id}",
                )
                self.automation_tasks[room.anchor_id] = task
                task.add_done_callback(
                    lambda finished, uid=room.anchor_id: self._automation_finished(
                        uid, finished
                    )
                )

        if self.watch_uid is not None and self.watch_uid not in self.live_rooms:
            await self._stop_watch("stream_ended")
        self._start_next_watch()

    async def _poll_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except BilibiliError:
                self.logger.exception(
                    "Live-state refresh failed; retrying on the next poll"
                )
            await self._wait_for_next_poll(stop_event)

    async def _stop_on_request(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()
        self.stopping = True
        raise _StopRequested

    async def _suspend_live_tasks(self) -> None:
        self.live_rooms.clear()
        tasks = list(self.automation_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.automation_tasks.clear()
        await self._stop_watch("pending", wake=False)

    async def shutdown(self) -> None:
        tasks = list(self.automation_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.automation_tasks.clear()
        await self._stop_watch("pending")
        self.state_store.save(self.state)

    async def _run_automation(self, room: LiveRoom, task_day: date) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._run_likes(room, task_day))
            tasks.create_task(self._run_danmaku(room, task_day))
        progress = self._current_room_progress(task_day, room.anchor_id)
        if progress and self._automation_complete(progress):
            if not self._automation_uncertain(progress):
                progress.last_error = None
            progress.updated_at = self.wall_time()
            self._queue_completion(progress)

    async def _run_likes(self, room: LiveRoom, task_day: date) -> None:
        while True:
            progress = self._current_room_progress(task_day, room.anchor_id)
            if (
                not progress
                or progress.like_attempts >= self.settings.like_request_count
            ):
                return
            if not self._is_live(task_day, room.anchor_id):
                return
            progress.like_attempts += 1
            progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
            try:
                await self.client.like(room, self.settings.like_clicks_per_request)
            except asyncio.CancelledError:
                raise
            except BilibiliError as error:
                ambiguous = error.ambiguous
                if not ambiguous:
                    progress.like_attempts -= 1
                title = (
                    "Like task outcome uncertain" if ambiguous else "Like task failed"
                )
                self._record_automation_error(progress, "like", title, error)
                return
            progress.likes_sent += 1
            if not self._automation_uncertain(progress):
                progress.last_error = None
            progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
            self.logger.info(
                "Like batch %s/%s confirmed for %s (%s clicks)",
                progress.like_attempts,
                self.settings.like_request_count,
                _streamer(room),
                self.settings.like_clicks_per_request,
            )
            if progress.like_attempts < self.settings.like_request_count:
                await self.sleep(self.settings.like_interval_seconds)

    async def _run_danmaku(self, room: LiveRoom, task_day: date) -> None:
        while True:
            progress = self._current_room_progress(task_day, room.anchor_id)
            if not progress or progress.danmaku_attempts >= self.settings.danmaku_count:
                return
            if not self._is_live(task_day, room.anchor_id):
                return
            await self.danmaku_gate.wait()
            if not self._is_live(task_day, room.anchor_id):
                return
            progress.danmaku_attempts += 1
            progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
            try:
                message = await self.client.send_danmaku(room)
            except asyncio.CancelledError:
                raise
            except BilibiliError as error:
                ambiguous = error.ambiguous
                if not ambiguous:
                    progress.danmaku_attempts -= 1
                title = (
                    "Danmaku task outcome uncertain"
                    if ambiguous
                    else "Danmaku task failed"
                )
                self._record_automation_error(progress, "danmaku", title, error)
                return
            progress.danmaku_sent += 1
            if not self._automation_uncertain(progress):
                progress.last_error = None
            progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
            self.logger.info(
                "Danmaku %s/%s sent for %s: %s",
                progress.danmaku_attempts,
                self.settings.danmaku_count,
                _streamer(room),
                message,
            )
            if progress.danmaku_attempts < self.settings.danmaku_count:
                await self.sleep(self.settings.danmaku_interval_seconds)

    def _start_next_watch(self) -> None:
        if self.watch_task is not None or self.settings.watch_minutes == 0:
            return
        for anchor_id in self.settings.watch_uids:
            room = self.live_rooms.get(anchor_id)
            if not room:
                continue
            progress = self.state.watches.get(anchor_id)
            if progress and progress.status in {"completed", "uncertain"}:
                continue
            if not progress:
                progress = WatchProgress(
                    anchor_id=room.anchor_id,
                    room_id=room.room_id,
                    anchor_name=room.anchor_name,
                    updated_at=self.wall_time(),
                )
                self.state.watches[anchor_id] = progress
            else:
                progress.room_id = room.room_id
                progress.anchor_name = room.anchor_name
                progress.status = "pending"
                progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
            self.watch_uid = anchor_id
            task = asyncio.create_task(
                self._watch_live(room, self.state.day), name=f"watch-{anchor_id}"
            )
            self.watch_task = task
            task.add_done_callback(
                lambda finished, uid=anchor_id: self._watch_finished(uid, finished)
            )
            self.logger.info("Started priority watch for %s", _streamer(room))
            return

    async def _watch_live(self, room: LiveRoom, task_day: date) -> None:
        progress = self.state.watches[room.anchor_id]
        progress.status = "running"
        if progress.watch_seconds_attempted == progress.watched_seconds:
            progress.last_error = None
        progress.updated_at = self.wall_time()
        self.state_store.save(self.state)
        session = self.client.new_heartbeat_session(room)
        target_seconds = self.settings.watch_minutes * 60
        while progress.watch_seconds_attempted < target_seconds:
            if not self._is_live(task_day, room.anchor_id):
                progress.status = "stream_ended"
                progress.updated_at = self.wall_time()
                self.state_store.save(self.state)
                return
            watch_seconds = min(
                self.settings.heartbeat_interval_seconds,
                target_seconds - progress.watch_seconds_attempted,
            )
            await self.sleep(watch_seconds)
            if not self._is_live(task_day, room.anchor_id):
                progress.status = "stream_ended"
                progress.updated_at = self.wall_time()
                self.state_store.save(self.state)
                return
            progress.watch_seconds_attempted += watch_seconds
            progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
            try:
                await self.client.heartbeat(room, session, watch_seconds)
            except asyncio.CancelledError:
                raise
            except BilibiliError as error:
                ambiguous = error.ambiguous
                if not ambiguous:
                    progress.watch_seconds_attempted -= watch_seconds
                progress.status = "pending"
                progress.last_error = _safe_error(error)
                progress.updated_at = self.wall_time()
                self._queue_notification(
                    sequence_id=(
                        f"bilibili-{self.state.day.isoformat()}-"
                        f"{progress.anchor_id}-watch-error"
                    ),
                    title=f"「{progress.anchor_name}」 Watch heartbeat failed",
                    message=f"UID: {progress.anchor_id}\n{progress.last_error}",
                    tags="warning",
                )
                self.logger.error(
                    "Watch heartbeat failed for %s: %s",
                    _streamer(room),
                    progress.last_error,
                )
                return
            progress.heartbeat_count += 1
            progress.watched_seconds += watch_seconds
            if progress.watch_seconds_attempted == progress.watched_seconds:
                progress.last_error = None
            progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
            self.logger.info(
                "Watch progress %s/%s minutes confirmed for %s (heartbeat %s)",
                _format_minutes(progress.watched_seconds),
                self.settings.watch_minutes,
                _streamer(room),
                progress.heartbeat_count,
            )
        progress.status = (
            "uncertain"
            if progress.watch_seconds_attempted > progress.watched_seconds
            else "completed"
        )
        progress.updated_at = self.wall_time()
        self.state_store.save(self.state)

    async def _stop_watch(self, status: str, *, wake: bool = True) -> None:
        task = self.watch_task
        anchor_id = self.watch_uid
        if task is None or anchor_id is None:
            return
        progress = self.state.watches.get(anchor_id)
        if progress and progress.status not in {"completed", "uncertain"}:
            progress.status = status
            progress.updated_at = self.wall_time()
            self.state_store.save(self.state)
        self.watch_task = None
        self.watch_uid = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if wake:
            self.wake_event.set()

    def _watch_finished(self, anchor_id: int, task: asyncio.Task[None]) -> None:
        if self.watch_task is not task:
            return
        self.watch_task = None
        self.watch_uid = None
        if not task.cancelled() and (error := task.exception()):
            self.logger.error(
                "Watch task failed for UID %s: %s", anchor_id, _safe_error(error)
            )
            self._fail_background(error)
        progress = self.state.watches.get(anchor_id)
        if not progress or progress.status != "pending":
            self.wake_event.set()

    def _automation_finished(self, anchor_id: int, task: asyncio.Task[None]) -> None:
        if self.automation_tasks.get(anchor_id) is task:
            self.automation_tasks.pop(anchor_id, None)
        if not task.cancelled() and (error := task.exception()):
            self.logger.error(
                "Automation task failed for UID %s: %s", anchor_id, _safe_error(error)
            )
            self._fail_background(error)

    async def _ensure_day(self, current_day: date) -> None:
        if current_day != self.state.day:
            await self._rollover(current_day)

    async def _rollover(self, new_day: date) -> None:
        async with self.rollover_lock:
            if new_day == self.state.day:
                return
            tasks = list(self.automation_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.automation_tasks.clear()
            await self._stop_watch("day_ended")

            old_day = self.state.day
            for progress in self.state.watches.values():
                if progress.status in {"pending", "running"}:
                    progress.status = "day_ended"
            if self.outbox:
                self.outbox.queue(
                    OutboxMessage(
                        sequence_id=f"bilibili-watch-{old_day.isoformat()}",
                        title="Daily watch summary",
                        message=_watch_summary(
                            self.state.watches.values(),
                            old_day,
                            self.settings.watch_minutes,
                        ),
                        tags="eyes",
                    )
                )
            outbox = self.state.outbox
            self.state = AppState(day=new_day, outbox=outbox)
            self.live_rooms.clear()
            self.state_store.save(self.state)
            if self.outbox:
                self.outbox.wake()
            self.wake_event.set()
            self.logger.info("Rolled daily task state from %s to %s", old_day, new_day)

    async def _midnight_loop(self) -> None:
        while True:
            now = self.now()
            next_midnight = datetime.combine(
                now.date() + timedelta(days=1), clock_time(), SHANGHAI
            )
            await self.sleep(max(0.0, (next_midnight - now).total_seconds()))
            await self._rollover(next_midnight.date())

    def _room_progress(self, room: LiveRoom) -> RoomProgress:
        progress = self.state.rooms.get(room.anchor_id)
        if not progress:
            progress = RoomProgress(
                anchor_id=room.anchor_id,
                room_id=room.room_id,
                anchor_name=room.anchor_name,
                updated_at=self.wall_time(),
            )
            self.state.rooms[room.anchor_id] = progress
        else:
            progress.room_id = room.room_id
            progress.anchor_name = room.anchor_name
        return progress

    def _current_room_progress(
        self, task_day: date, anchor_id: int
    ) -> RoomProgress | None:
        if self.state.day != task_day:
            return None
        return self.state.rooms.get(anchor_id)

    def _automation_complete(self, progress: RoomProgress) -> bool:
        return (
            progress.like_attempts >= self.settings.like_request_count
            and progress.danmaku_attempts >= self.settings.danmaku_count
        )

    def _automation_uncertain(self, progress: RoomProgress) -> bool:
        return (
            progress.like_attempts > progress.likes_sent
            or progress.danmaku_attempts > progress.danmaku_sent
        )

    def _is_live(self, task_day: date, anchor_id: int) -> bool:
        return (
            self.state.day == task_day
            and anchor_id in self.live_rooms
            and not self.stopping
        )

    def _record_automation_error(
        self,
        progress: RoomProgress,
        phase: str,
        title: str,
        error: Exception,
    ) -> None:
        progress.last_error = _safe_error(error)
        progress.updated_at = self.wall_time()
        self._queue_notification(
            sequence_id=(
                f"bilibili-{self.state.day.isoformat()}-"
                f"{progress.anchor_id}-{phase}-error"
            ),
            title=f"「{progress.anchor_name}」 {title}",
            message=f"UID: {progress.anchor_id}\n{progress.last_error}",
            tags="warning",
        )
        self.logger.error(
            "%s for %s: %s", title, _streamer(progress), progress.last_error
        )

    def _queue_completion(self, progress: RoomProgress) -> None:
        if not self.notifier or progress.notification_queued:
            return
        uncertain_likes = progress.like_attempts - progress.likes_sent
        uncertain_danmaku = progress.danmaku_attempts - progress.danmaku_sent
        uncertain = uncertain_likes > 0 or uncertain_danmaku > 0
        if uncertain:
            message = (
                f"UID: {progress.anchor_id}\n"
                f"Confirmed: {progress.likes_sent * self.settings.like_clicks_per_request} live likes "
                f"and {progress.danmaku_sent} Danmaku.\n"
                f"Uncertain: {uncertain_likes} like batches and "
                f"{uncertain_danmaku} Danmaku requests."
            )
        else:
            message = (
                f"UID: {progress.anchor_id}\n"
                f"{progress.likes_sent * self.settings.like_clicks_per_request} live likes "
                f"and {progress.danmaku_sent} Danmaku sent."
            )
        progress.notification_queued = True
        progress.updated_at = self.wall_time()
        self._queue_notification(
            sequence_id=(
                f"bilibili-{self.state.day.isoformat()}-"
                f"{progress.anchor_id}-automation-complete"
            ),
            title=(
                f"「{progress.anchor_name}」 Automatic task outcome uncertain"
                if uncertain
                else f"「{progress.anchor_name}」 Automatic task completed"
            ),
            message=message,
            tags="warning" if uncertain else "white_check_mark",
        )

    def _queue_notification(
        self, *, sequence_id: str, title: str, message: str, tags: str
    ) -> None:
        if self.outbox:
            self.outbox.queue(
                OutboxMessage(
                    sequence_id=sequence_id,
                    title=title,
                    message=message,
                    tags=tags,
                )
            )
        self.state_store.save(self.state)

    async def _raise_background_failure(self) -> None:
        if self.fatal_error is None:
            return
        await self.fatal_error

    def _fail_background(self, error: BaseException) -> None:
        if self.fatal_error and not self.fatal_error.done():
            self.fatal_error.set_exception(error)

    async def _wait_for_next_poll(self, stop_event: asyncio.Event) -> None:
        stop_waiter = asyncio.create_task(stop_event.wait())
        wake_waiter = asyncio.create_task(self.wake_event.wait())
        done, pending = await asyncio.wait(
            {stop_waiter, wake_waiter},
            timeout=self.settings.poll_interval_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if wake_waiter in done:
            self.wake_event.clear()


class MinimumInterval:
    def __init__(self, interval_seconds: float, sleep: Sleep):
        self.interval_seconds = interval_seconds
        self.sleep = sleep
        self.lock = asyncio.Lock()
        self.next_available = 0.0

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            scheduled_at = max(now, self.next_available)
            await self.sleep(max(0.0, scheduled_at - now))
            self.next_available = scheduled_at + self.interval_seconds


def _streamer(room: LiveRoom | RoomProgress) -> str:
    return f"{room.anchor_name} (UID: {room.anchor_id})"


def _watch_summary(
    results: Iterable[WatchProgress], summary_date: date, target_minutes: int
) -> str:
    values = list(results)
    lines = [f"Date: {summary_date.isoformat()}", f"Attempted: {len(values)}"]
    for progress in values:
        status = progress.status.replace("_", " ")
        detail = (
            f"{_format_minutes(progress.watched_seconds)}/{target_minutes} "
            f"confirmed minutes, {status}"
        )
        uncertain_seconds = progress.watch_seconds_attempted - progress.watched_seconds
        if uncertain_seconds:
            detail += f", {_format_minutes(uncertain_seconds)} minutes uncertain"
        if progress.last_error:
            detail += f", {progress.last_error}"
        lines.append(f"- {_streamer(progress)}: {detail}")
    return "\n".join(lines)


def _safe_error(error: Exception) -> str:
    value = str(error).strip()
    return value if value else type(error).__name__


def _format_minutes(seconds: int) -> str:
    if seconds % 60 == 0:
        return str(seconds // 60)
    return f"{seconds / 60:.1f}"
