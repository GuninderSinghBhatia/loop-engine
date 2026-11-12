"""Control channel: how the CLI sends signals to the engine (Phase 2).

Signals: pause, resume, stop, steer(text), approval(yes|no).
"""

from queue import Empty, Queue

URGENT_KINDS = {"pause", "resume", "stop", "approval"}


class ControlChannel:
    def __init__(self) -> None:
        self.normal_queue: Queue = Queue()
        self.priority_queue: Queue = Queue()

    def send(self, kind: str, payload: str | None = None) -> None:
        item = (kind, payload)
        if kind in URGENT_KINDS:
            self.priority_queue.put(item)
        else:
            self.normal_queue.put(item)

    def receive(self) -> tuple[str, str | None] | None:
        """Return the next (kind, payload), or None if nothing is waiting."""
        for q in (self.priority_queue, self.normal_queue):
            try:
                return q.get_nowait()
            except Empty:
                continue
        return None
