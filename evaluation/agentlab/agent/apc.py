from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class FocusState:
    subtask_id: str
    active_seconds: float = 0
    started_at: float = 0
    running: bool = True


class APCMonitor:
    """Objective active-wall-time monitor; stall signals are diagnostic only."""

    def __init__(self, timeout_seconds: float = 300, clock: Callable[[], float] = time.monotonic):
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.focus: FocusState | None = None

    def set_focus(self, subtask_id: str | None) -> None:
        if not subtask_id:
            self.focus = None
            return
        if self.focus and self.focus.subtask_id == subtask_id:
            return
        self.focus = FocusState(subtask_id=subtask_id, started_at=self.clock())

    def elapsed(self) -> float:
        if not self.focus:
            return 0
        current = self.focus.active_seconds
        if self.focus.running:
            current += self.clock() - self.focus.started_at
        return current

    def pause(self) -> None:
        if self.focus and self.focus.running:
            self.focus.active_seconds = self.elapsed()
            self.focus.running = False

    def resume(self) -> None:
        if self.focus and not self.focus.running:
            self.focus.started_at = self.clock()
            self.focus.running = True

    def should_intervene(self) -> bool:
        return bool(self.focus and self.elapsed() >= self.timeout_seconds)

    def snapshot(self) -> dict[str, float | str | bool | None]:
        return {
            "focused_subtask": self.focus.subtask_id if self.focus else None,
            "active_seconds": round(self.elapsed(), 3),
            "timeout_seconds": self.timeout_seconds,
            "timed_out": self.should_intervene(),
        }

