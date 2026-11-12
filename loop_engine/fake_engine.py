"""A pretend engine that emits scripted events (Phase 2).

Lets us build and judge the CLI before the real engine exists.
"""

import time
from collections.abc import Callable, Iterator

from loop_engine.control import ControlChannel
from loop_engine.events import Event

SCRIPT = [
    ("phase_started", "planning: writing the spec"),
    ("phase_started", "building"),
    ("file_written", "solution.py"),
    ("command_run", "ruff check"),
    ("command_run", "pytest"),
    ("test_result", "3 passed"),
]


NEEDS_APPROVAL = {"file_written", "command_run"}


class FakeEngine:
    def __init__(
        self,
        channel: ControlChannel,
        delay: float = 0.0,
        ask_approval: Callable[[], bool] = lambda: False,
    ) -> None:
        self.channel = channel
        self.delay = delay
        self.ask_approval = ask_approval  # asked at each step, so it can change mid-run

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

    def _checkpoint(self) -> Iterator[Event]:
        """Safe point between steps: read control signals. Returns True to stop."""
        paused = False
        while True:
            msg = self.channel.receive()
            if msg is None:
                if not paused:
                    return False
                time.sleep(0.05)
                continue
            kind, payload = msg
            if kind == "stop":
                return True
            if kind == "pause" and not paused:
                paused = True
                yield Event(kind="paused")
            elif kind == "resume" and paused:
                paused = False
                yield Event(kind="resumed")
            elif kind == "steer":
                yield Event(kind="steer_received", text=payload or "")

    def _await_approval(self, what: str) -> Iterator[Event]:
        """Ask for approval and wait for the answer. Returns "yes", "no" or "stop"."""
        yield Event(kind="approval_needed", text=what)
        while True:
            msg = self.channel.receive()
            if msg is None:
                time.sleep(0.05)
                continue
            kind, payload = msg
            if kind == "approval":
                return "yes" if payload == "yes" else "no"
            if kind == "stop":
                return "stop"
            if kind == "steer":
                yield Event(kind="steer_received", text=payload or "")
