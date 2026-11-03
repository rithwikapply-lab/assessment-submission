import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int:
        ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)


class ManualClock:
    def __init__(self, initial_ms: int = 0) -> None:
        self._current_ms = initial_ms

    def now_ms(self) -> int:
        return self._current_ms

    def advance(self, ms: int) -> None:
        self._current_ms += ms

    def set(self, ms: int) -> None:
        self._current_ms = ms
