import asyncio
import logging
import re
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol, Self

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from .state import AppState, OutboxMessage, StateStore

SEQUENCE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")


class NotificationError(RuntimeError):
    pass


class NotificationPublisher(Protocol):
    async def publish(
        self, title: str, message: str, *, tags: str, sequence_id: str
    ) -> None: ...


class NtfyNotifier(AbstractAsyncContextManager["NtfyNotifier"]):
    def __init__(self, endpoint: str, token: str | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self.session = AsyncSession(max_clients=1, timeout=10, trust_env=True)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def publish(
        self, title: str, message: str, *, tags: str, sequence_id: str
    ) -> None:
        if not self.session:
            raise RuntimeError("ntfy notifier is not started")
        validate_sequence_id(sequence_id)
        headers = {"Title": title, "Tags": tags, "X-Sequence-ID": sequence_id}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = await self.session.post(
                self.endpoint, data=message.encode(), headers=headers
            )
            response.raise_for_status()
        except RequestsError as error:
            raise NotificationError(
                f"ntfy delivery failed: {type(error).__name__}"
            ) from error


def validate_sequence_id(sequence_id: str) -> None:
    if not SEQUENCE_ID_PATTERN.fullmatch(sequence_id):
        raise ValueError(
            "ntfy sequence ID must contain only letters, numbers, underscores, and hyphens"
        )


StateProvider = Callable[[], AppState]
Clock = Callable[[], float]


class PersistentOutbox:
    def __init__(
        self,
        notifier: NotificationPublisher,
        state_store: StateStore,
        state: StateProvider,
        *,
        wall_time: Clock = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        self.notifier = notifier
        self.state_store = state_store
        self.state = state
        self.wall_time = wall_time
        self.logger = logger or logging.getLogger(__name__)
        self.event = asyncio.Event()

    def queue(self, message: OutboxMessage) -> None:
        validate_sequence_id(message.sequence_id)
        self.state().outbox.setdefault(message.sequence_id, message)
        self.event.set()

    def wake(self) -> None:
        self.event.set()

    async def run(self) -> None:
        while True:
            self.event.clear()
            await self.flush_once()
            if self.event.is_set():
                continue
            try:
                await asyncio.wait_for(self.event.wait(), timeout=self._next_delay())
            except TimeoutError:
                pass

    async def flush_once(self) -> None:
        for sequence_id, pending in list(self.state().outbox.items()):
            if pending.next_attempt_at > self.wall_time():
                continue
            try:
                await self.notifier.publish(
                    pending.title,
                    pending.message,
                    tags=pending.tags,
                    sequence_id=pending.sequence_id,
                )
            except asyncio.CancelledError:
                raise
            except NotificationError as error:
                pending.attempts += 1
                pending.next_attempt_at = self.wall_time() + min(
                    900, 30 * 2 ** min(pending.attempts - 1, 5)
                )
                self.state_store.save(self.state())
                self.logger.warning(
                    "Notification %s remains in outbox after %s: %s",
                    sequence_id,
                    pending.attempts,
                    type(error).__name__,
                )
                continue
            state = self.state()
            if state.outbox.get(sequence_id) is pending:
                state.outbox.pop(sequence_id, None)
                self.state_store.save(state)
                self.logger.info("Notification %s delivered", sequence_id)

    def _next_delay(self) -> float:
        next_attempts = [
            message.next_attempt_at for message in self.state().outbox.values()
        ]
        if not next_attempts:
            return 3600
        return max(0.0, min(next_attempts) - self.wall_time())
