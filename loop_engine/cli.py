"""Entry point for the `loop` command: an interactive session.

Idle: typed text is a new project prompt. Running: typed text is steering, or the
answer to a question the engine asked. Slash commands become control signals.
"""

import sys
import threading
from collections.abc import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape

from loop_engine.control import ControlChannel
from loop_engine.engine import Engine
from loop_engine.events import Event
from loop_engine.fake_engine import FakeEngine
from loop_engine.launcher import (
    AppProcess,
    load_run_spec,
    open_browser,
    open_in_editor,
    wait_for_url,
)
from loop_engine.vcs import push

STYLES = {
    "phase_started": "bold cyan",
    "intake_question": "bold yellow",
    "brief_ready": "bold blue",
    "repo_ready": "green",
    "remote_set": "green",
    "spec_written": "bold blue",
    "tasks_created": "bold blue",
    "agent_started": "cyan",
    "agent_done": "cyan",
    "files_collected": "green",
    "conflict": "yellow",
    "file_written": "green",
    "command_run": "dim",
    "test_result": "bold green",
    "steer_received": "yellow",
    "paused": "bold yellow",
    "resumed": "bold yellow",
    "stuck": "bold red",
    "loop_done": "bold magenta",
    "approval_needed": "bold yellow",
    "step_skipped": "dim yellow",
    "rewound": "magenta",
    "error": "bold red",
}

YES_NO = {"y": "yes", "yes": "yes", "n": "no", "no": "no"}

HELP = (
    "/pause  /resume  /stop   control the running loop (Ctrl-C also pauses)\n"
    "/skip                    skip the questions and start building right away\n"
    "/approve on|off          ask y/n before files are written or commands run\n"
    "/status                  what is happening now\n"
    "/run  /run stop          start or stop the built project\n"
    "/browser                 open the built web app in the browser\n"
    "/open                    open the project folder in your editor (LOOP_EDITOR)\n"
    "/push                    push the project to its GitHub repo\n"
    "/help  /exit\n"
    "While a run is active, typed text answers the engine's question or steers the build.\n"
    "When idle, typed text starts a new project."
)

EngineFactory = Callable[[ControlChannel, Callable[[], bool]], Engine | FakeEngine]


class Session:
    def __init__(
        self,
        console: Console,
        fake: bool = False,
        delay: float = 0.8,
        engine_factory: EngineFactory | None = None,
    ) -> None:
        self.console = console
        self.fake = fake
        self.delay = delay
        self.engine_factory = engine_factory
        self.channel = ControlChannel()
        self.thread: threading.Thread | None = None
        self.last_event: Event | None = None
        self.should_exit = False
        self.approve = False
        self.awaiting_approval = False
        self.awaiting_answer = False
        self.paused = False
        self.project: str | None = None  # the folder the current/last run builds into
        self.app: AppProcess | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def say(self, text: str) -> None:
        self.console.print(text)

    # ---- showing events ----

    def render(self, event: Event) -> None:
        if event.kind == "approval_needed":
            self.awaiting_approval = True
        elif event.kind == "intake_question":
            self.awaiting_answer = True
        elif event.kind == "paused":
            self.paused = True
        elif event.kind == "resumed":
            self.paused = False
        elif event.kind == "repo_ready":
            self.project = event.text
        style = STYLES.get(event.kind, "white")
        suffix = "  [bold](y/n)[/bold]" if event.kind == "approval_needed" else ""
        # escape() so text the user typed (e.g. "[red]hi") prints literally
        self.say(f"[{style}]{event.kind}[/{style}] {escape(event.text)}{suffix}")
        if event.kind == "loop_done":
            self.paused = self.awaiting_answer = self.awaiting_approval = False
            if event.text.startswith("success"):
                self.after_success()

    def after_success(self) -> None:
        """Summarise, and launch the project if it has a UI."""
        if not self.project:
            return
        self.say(f"[bold green]Built in[/bold green] {escape(self.project)}")
        spec = load_run_spec(self.project)
        if spec is None or not spec.has_ui:
            return
        if self.approve:
            self.say("It has a UI. Approvals are on, so it was not launched: use /run.")
        else:
            self.start_app(auto=True)

    # ---- running the engine ----

    def start_run(self, prompt: str) -> None:
        self.channel = ControlChannel()  # fresh channel: no leftovers from last run
        self.awaiting_approval = self.awaiting_answer = self.paused = False
        self.project = None

        def ask() -> bool:  # read live, so /approve works mid-run
            return self.approve

        if self.engine_factory:
            engine = self.engine_factory(self.channel, ask)
        elif self.fake:
            engine = FakeEngine(self.channel, delay=self.delay, ask_approval=ask)
        else:
            engine = Engine(self.channel, ask_approval=ask, intake=True, ask_when_stuck=True)
        self.thread = threading.Thread(target=self._run, args=(engine, prompt), daemon=True)
        self.thread.start()

    def _run(self, engine: Engine | FakeEngine, prompt: str) -> None:
        try:
            for event in engine.run(prompt):
                self.last_event = event
                self.render(event)
        except Exception as exc:  # noqa: BLE001 - the session must survive an engine crash
            self.render(Event(kind="error", text=f"{type(exc).__name__}: {exc}"))
            if self.project:
                self.say(f"Details saved in {escape(self.project)}/.loop/error.txt")

    # ---- input ----

    def handle_line(self, line: str) -> None:
        line = line.strip()
        if line.startswith("/"):
            self.handle_command(line[1:].lower())
        elif self.running and self.awaiting_answer:  # a blank line is a valid answer
            self.awaiting_answer = False
            self.channel.send("answer", line)
        elif not line:
            return
        elif self.running and self.awaiting_approval and line.lower() in YES_NO:
            self.awaiting_approval = False  # clear first: the engine may ask again at once
            self.channel.send("approval", YES_NO[line.lower()])
        elif self.running:
            self.channel.send("steer", line)
        else:
            self.start_run(line)

    def handle_command(self, cmd: str) -> None:
        parts = cmd.split()
        if not parts:
            self.say("Type a command after the slash. Try /help.")
            return
        name, args = parts[0], parts[1:]
        if name == "approve":
            if args and args[0] in ("on", "off"):
                self.approve = args[0] == "on"
            self.say(f"Approval is {'on' if self.approve else 'off'}.")
        elif name in ("pause", "resume", "stop"):
            self.control(name)
        elif name == "skip":
            if self.running and self.awaiting_answer:
                self.awaiting_answer = False
                self.channel.send("skip")
            else:
                self.say("Nothing to skip.")
        elif name == "status":
            self.status()
        elif name == "run":
            if args[:1] == ["stop"]:
                self.stop_app()
            else:
                self.start_app()
        elif name == "browser":
            self.browser()
        elif name == "open":
            self.open_project()
        elif name == "push":
            self.push_project()
        elif name == "help":
            self.say(escape(HELP))
        elif name == "exit":
            if self.running:
                self.channel.send("stop")
                self.thread.join(timeout=3)
            self.stop_app(quiet=True)
            self.should_exit = True
        else:
            self.say(f"Unknown command: /{escape(name)}. Try /help.")

    def control(self, name: str) -> None:
        if not self.running:
            self.say("Nothing is running.")
        elif name == "pause" and self.paused:
            self.say("Already paused.")
        elif name == "resume" and not self.paused:
            self.say("Not paused.")
        else:
            self.channel.send(name)

    def status(self) -> None:
        if self.running:
            last = self.last_event.kind if self.last_event else "starting"
            self.say(f"Running ({'paused, ' if self.paused else ''}last: {last}).")
            if self.awaiting_answer:
                self.say("Waiting for your answer.")
            if self.awaiting_approval:
                self.say("Waiting for your approval (y/n).")
        else:
            self.say("Idle.")
        if self.project:
            self.say(f"Project: {escape(self.project)}")
        if self.app and self.app.running:
            self.say("The built project is running (/run stop to end it).")

    # ---- using what was built ----

    def start_app(self, auto: bool = False) -> None:
        if not self.project:
            self.say("No project yet. Build one first.")
            return
        spec = load_run_spec(self.project)
        if spec is None or spec.kind == "none" or not spec.command:
            self.say("Nothing to run: the build recorded no start command.")
            return
        if self.app and self.app.running:
            self.say("Already running. /run stop ends it.")
            return
        app = AppProcess(spec, self.project, lambda line: self.say(f"[dim]app>[/dim] {escape(line)}"))
        try:
            app.start()
        except (OSError, ValueError) as exc:
            self.say(f"[bold red]Could not start:[/bold red] {escape(str(exc))}")
            return
        self.app = app
        self.say(f"Started: {escape(spec.command)}")
        if spec.kind == "web" and spec.url:
            threading.Thread(target=self._when_up, args=(spec.url, auto), daemon=True).start()

    def _when_up(self, url: str, auto: bool) -> None:
        if not wait_for_url(url):
            self.say(f"Nothing answered at {escape(url)} after 20s. Check the app output above.")
        elif auto:
            open_browser(url)
            self.say(f"Opened {escape(url)} in your browser.")
        else:
            self.say(f"It is up at {escape(url)}. Type /browser to open it.")

    def stop_app(self, quiet: bool = False) -> None:
        if self.app and self.app.running:
            self.app.stop()
            if not quiet:
                self.say("Stopped the built project.")
        elif not quiet:
            self.say("The built project is not running.")

    def browser(self) -> None:
        spec = load_run_spec(self.project) if self.project else None
        if spec is None or not spec.url:
            self.say("This project has no web UI.")
        elif not (self.app and self.app.running):
            self.say("It is not running. Start it with /run first.")
        else:
            open_browser(spec.url)
            self.say(f"Opened {escape(spec.url)}.")

    def open_project(self) -> None:
        if not self.project:
            self.say("No project yet. Build one first.")
            return
        try:
            open_in_editor(self.project)
        except OSError as exc:
            self.say(f"[bold red]Could not open the editor:[/bold red] {escape(str(exc))}")

    def push_project(self) -> None:
        if not self.project:
            self.say("No project yet. Build one first.")
            return
        threading.Thread(target=self._push, args=(self.project,), daemon=True).start()

    def _push(self, project: str) -> None:
        self.say("Pushing to origin...")
        try:
            push(project)
            self.say("[bold green]Pushed.[/bold green]")
        except (RuntimeError, OSError) as exc:
            self.say(f"[bold red]Push failed:[/bold red] {escape(str(exc))}")


def main() -> None:
    console = Console()
    fake = "--fake" in sys.argv[1:]
    mode = "  [dim](fake engine)[/dim]" if fake else ""
    console.print(f"[bold green]Loop Engine v2[/bold green]{mode}  (type /help)")
    session = Session(console, fake=fake)
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
