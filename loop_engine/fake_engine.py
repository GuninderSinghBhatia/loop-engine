"""A pretend engine that emits scripted events (Phase 2).

Lets us build and judge the CLI before the real engine exists.
"""

import time
from collections.abc import Callable, Iterator

from loop_engine.control import ControlChannel
from loop_engine.events import Event
from loop_engine.steering import Steerable

SCRIPT = [
    ("phase_started", "planning: writing the spec"),
    ("phase_started", "building"),
    ("file_written", "solution.py"),
    ("command_run", "ruff check"),
    ("command_run", "pytest"),
    ("test_result", "3 passed"),
]


NEEDS_APPROVAL = {"file_written", "command_run"}


class FakeEngine(Steerable):
    def __init__(
        self,
        channel: ControlChannel,
        delay: float = 0.0,
        ask_approval: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(channel, ask_approval)
        self.delay = delay

    def run(self, prompt: str) -> Iterator[Event]:
        for kind, text in SCRIPT:
            stopped = yield from self._checkpoint()
            if stopped:
                yield Event(kind="loop_done", text="stopped")
                return
            if kind in NEEDS_APPROVAL and self.ask_approval():
                answer = yield from self._await_approval(f"{kind}: {text}")
                if answer == "stop":
                    yield Event(kind="loop_done", text="stopped")
                    return
                if answer == "no":
                    yield Event(kind="step_skipped", text=text)
                    continue
            time.sleep(self.delay)
            yield Event(kind=kind, text=text)
        yield Event(kind="loop_done", text="finished")
