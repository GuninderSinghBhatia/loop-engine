"""Events the engine emits. The engine never prints; it only emits these."""

from dataclasses import dataclass


@dataclass
class Event:
    kind: str  # e.g. "phase_started", "test_result", "steer_received", "loop_done"
    text: str = ""
