"""Read and atomically persist complete task state; invalid state stops execution."""

import os
from datetime import date
from pathlib import Path

from .progress import AppState


def read_state(path: Path) -> AppState:
    return AppState.model_validate_json(path.read_bytes())


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self, default_day: date) -> AppState:
        try:
            return read_state(self.path)
        except FileNotFoundError:
            return AppState(day=default_day)

    def save(self, state: AppState) -> None:
        # Execution mutates related fields together. Validate the whole snapshot here.
        snapshot = AppState.model_validate(state)
        payload = snapshot.model_dump_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with open(
            temporary_path,
            "w",
            encoding="utf-8",
            opener=lambda path, flags: os.open(path, flags, 0o600),
        ) as stream:
            os.chmod(temporary_path, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(self.path)
