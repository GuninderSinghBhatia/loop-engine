"""Use what was built: start it, open it in the browser, open it in the editor."""

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loop_engine.env import project_python

UI_KINDS = ("web", "desktop")  # what gets launched automatically after a successful build


@dataclass
class RunSpec:
    kind: str  # web | desktop | cli | none
    command: str | None = None
    url: str | None = None

    @property
    def has_ui(self) -> bool:
        return self.kind in UI_KINDS and bool(self.command)


def load_run_spec(workspace: str) -> RunSpec | None:
    """The start command the engine recorded in .loop/run.json at build time."""
    path = Path(workspace) / ".loop" / "run.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunSpec(kind=data["kind"], command=data.get("command"), url=data.get("url"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def build_command(command: str, python: str) -> list[str]:
    """Split the command, swapping a leading `python` for the project's own interpreter."""
    parts = shlex.split(command, posix=os.name != "nt")
    # Windows-style splitting keeps the quote characters around a token; drop them.
    parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    if parts and parts[0].lower() in ("python", "python3", "py"):
        parts[0] = python
    return parts


def wait_for_url(url: str, timeout: float = 20.0, interval: float = 0.3) -> bool:
    """Poll until the server answers (any HTTP response counts) or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True  # the server is up, it just answered with an error page
        except (urllib.error.URLError, OSError):
            time.sleep(interval)
    return False


def open_browser(url: str) -> bool:
    return webbrowser.open(url)


def open_in_editor(path: str) -> None:
    """Open the project folder in the user's editor (LOOP_EDITOR, default `code`)."""
    editor = os.environ.get("LOOP_EDITOR", "code")
    exe = shutil.which(editor)
    if exe is None:
        raise FileNotFoundError(f"editor '{editor}' not found; set LOOP_EDITOR to its command")
    subprocess.Popen([exe, path])


class AppProcess:
    """The built project running as a child process, its output streamed line by line."""

    def __init__(self, spec: RunSpec, workspace: str, on_line: Callable[[str], None]) -> None:
        self.spec = spec
        self.workspace = workspace
        self.on_line = on_line
        self.process: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if not self.spec.command:
            raise ValueError("this project has no start command")
        cmd = build_command(self.spec.command, project_python(self.workspace))
        self.process = subprocess.Popen(
            cmd,
            cwd=self.workspace,
            stdin=subprocess.DEVNULL,  # the session owns the keyboard
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self.on_line(line.rstrip("\n"))

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
