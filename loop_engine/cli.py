"""Entry point for the `loop` command: an interactive session (Phase 2).

Idle: typed text is a new project prompt. Running: typed text is steering.
Slash commands become control signals sent to the engine.
"""

import threading

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape

from loop_engine.control import ControlChannel
from loop_engine.events import Event
from loop_engine.fake_engine import FakeEngine

STYLES = {
    "phase_started": "bold cyan",
    "file_written": "green",
    "command_run": "dim",
    "test_result": "bold green",
    "steer_received": "yellow",
    "paused": "bold yellow",
    "resumed": "bold yellow",
    "loop_done": "bold magenta",
    "approval_needed": "bold yellow",
    "step_skipped": "dim yellow",
    "error": "bold red",
}

YES_NO = {"y": "yes", "yes": "yes", "n": "no", "no": "no"}

HELP = (
    "/pause  /resume  /stop   control the running loop\n"
    "/status                  what is happening now\n"
    "/help  /exit\n"
    "Type text while a run is active to steer it; when idle it starts a new run."
)


class Session:
    def __init__(self, console: Console, delay: float = 0.8) -> None:
        self.console = console
        self.delay = delay
        self.channel = ControlChannel()
        self.thread: threading.Thread | None = None
        self.last_event: Event | None = None
        self.should_exit = False
        self.approve = False
        self.awaiting_approval = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def render(self, event: Event) -> None:
        style = STYLES.get(event.kind, "white")
        # escape() so text the user typed (e.g. "[red]hi") prints literally
        if event.kind == "approval_needed":
            self.awaiting_approval = True
        suffix = "  [bold](y/n)[/bold]" if event.kind == "approval_needed" else ""
        self.console.print(f"[{style}]{event.kind}[/{style}] {escape(event.text)}{suffix}")

    def start_run(self, prompt: str) -> None:
        self.channel = ControlChannel()  # fresh channel: no leftovers from last run
        self.awaiting_approval = False
        engine = FakeEngine(self.channel, delay=self.delay, ask_approval=lambda: self.approve)
        self.thread = threading.Thread(target=self._run, args=(engine, prompt), daemon=True)
        self.thread.start()

    def _run(self, engine: FakeEngine, prompt: str) -> None:
        try:
            for event in engine.run(prompt):
                self.last_event = event
                self.render(event)
        except Exception as exc:  # noqa: BLE001 - the session must survive an engine crash
            self.render(Event(kind="error", text=str(exc)))

    def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line.startswith("/"):
            self.handle_command(line[1:].lower())
        elif self.running and self.awaiting_approval and line.lower() in YES_NO:
            self.awaiting_approval = False  # clear first: the engine may ask again at once
            self.channel.send("approval", YES_NO[line.lower()])
        elif self.running:
            self.channel.send("steer", line)
        else:
            self.start_run(line)

    def handle_command(self, cmd: str) -> None:
        name, *args = cmd.split()
        if name == "approve":
            if args and args[0] in ("on", "off"):
                self.approve = args[0] == "on"
            self.console.print(f"Approval is {'on' if self.approve else 'off'}.")
        elif name in ("pause", "resume", "stop"):
            if not self.running:
                self.console.print("Nothing is running.")
            else:
                self.channel.send(name)
        elif name == "status":
            if self.running and self.last_event:
                self.console.print(f"Running. Last: {self.last_event.kind}")
            else:
                self.console.print("Running." if self.running else "Idle.")
            if self.awaiting_approval:
                self.console.print("Waiting for your approval (y/n).")
        elif name == "help":
            self.console.print(escape(HELP))
        elif name == "exit":
            if self.running:
                self.channel.send("stop")
                self.thread.join(timeout=3)
            self.should_exit = True
        else:
            self.console.print(f"Unknown command: /{escape(name)}. Try /help.")


def main() -> None:
    console = Console()
    console.print("[bold green]Loop Engine v2[/bold green]  (type /help)")
    session = Session(console)
    prompt = PromptSession()
    with patch_stdout():
        while not session.should_exit:
            try:
                line = prompt.prompt("loop> ")
            except KeyboardInterrupt:  # Ctrl-C pauses a running loop
                session.handle_command("pause" if session.running else "exit")
                continue
            except EOFError:  # Ctrl-D
                session.handle_command("exit")
                continue
            session.handle_line(line)
