"""A pretend engine that emits scripted events (Phase 2).

Lets us build and judge the CLI before the real engine exists.
"""

from collections.abc import Iterator

from loop_engine.control import ControlChannel
from loop_engine.events import Event


class FakeEngine:
    def __init__(self, channel: ControlChannel) -> None:
        self.channel = channel

    def run(self, prompt: str) -> Iterator[Event]:
        yield Event(kind="phase_started", text="planning")
        msg = self.channel.receive()
        if msg is not None and msg[0] == "stop":
            yield Event(kind="loop_done", text="stopped")
            return
        if msg is not None and msg[0] == "steer":
            yield Event(kind="steer_received", text=msg[1] or "")
        yield Event(kind="test_result", text="all tests passed")
        yield Event(kind="loop_done", text="finished")