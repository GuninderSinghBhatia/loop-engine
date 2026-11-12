"""Shared by every engine: read control signals at safe points.

An engine calls `yield from self._checkpoint()` between steps. That is where
pause, stop and steer take effect, and where approvals are asked for.
"""

import time
from collections.abc import Callable, Iterator

from loop_engine.control import ControlChannel
from loop_engine.events import Event

SKIP = object()  # the user typed /skip while a question was open
STOP = object()  # the user typed /stop while a question was open


class Steerable:
    def __init__(
        self,
        channel: ControlChannel,
        ask_approval: Callable[[], bool] = lambda: False,
    ) -> None:
        self.channel = channel
        self.ask_approval = ask_approval  # asked at each step, so it can change mid-run
        self.steer_notes: list[str] = []  # every steering message so far, in order

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
                self.steer_notes.append(payload or "")
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
                self.steer_notes.append(payload or "")
                yield Event(kind="steer_received", text=payload or "")

    def _gate(self, what: str) -> Iterator[Event]:
        """Ask for approval only if approvals are on. Returns "yes", "no" or "stop"."""
        if not self.ask_approval():
            return "yes"
        return (yield from self._await_approval(what))

    def _ask(self, question: str) -> Iterator[Event]:
        """Ask the user a question and wait. Returns their text, SKIP or STOP."""
        yield Event(kind="intake_question", text=question)
        while True:
            msg = self.channel.receive()
            if msg is None:
                time.sleep(0.05)
                continue
            kind, payload = msg
            if kind == "answer":
                return payload or ""
            if kind == "skip":
                return SKIP
            if kind == "stop":
                return STOP
