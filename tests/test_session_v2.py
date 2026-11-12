import io
import json
import sys
import time

import pytest
from rich.console import Console

from loop_engine import cli
from loop_engine.cli import Session
from loop_engine.engine import Engine

FENCE = "`" * 3


def fenced(lang, body, path=None):
    tag = f"{lang} path={path}" if path else lang
    return f"{FENCE}{tag}\n{body}\n{FENCE}"


PLAN = fenced("markdown", "# spec") + "\n" + fenced("python", "def test_x():\n    pass", path="test_app.py")
SPLIT = fenced(
    "json",
    json.dumps(
        {
            "tasks": [{"id": "a", "title": "A", "files": ["app.py"], "depends_on": [], "brief": "write app"}],
            "run": {"kind": "web", "command": "python app.py", "url": "http://127.0.0.1:5000"},
        }
    ),
)
PASS = {"passed": True, "output": "1 passed in 0.01s", "timed_out": False}


def model(role, text):
    if role == "intake":
        return "1. Web or desktop?"
    if role == "planner":
        return PLAN
    if role == "splitter":
        return SPLIT
    return fenced("python", "print('built')", path="app.py")


def engine_for(channel, ask, model_fn=model, intake=True):
    return Engine(
        channel,
        ask_approval=ask,
        model=model_fn,
        check_ruff=lambda ws: {"passed": True, "output": "", "timed_out": False},
        check_pytest=lambda ws, py=None: PASS,
        prepare_env=lambda ws: sys.executable,
        intake=intake,
    )


def real_engine_session(out, approve=False):
    session = Session(Console(file=out, width=120), engine_factory=engine_for)
    session.approve = approve
    return session


def fake_session(out, **kw):
    return Session(Console(file=out, width=100), fake=True, **kw)


def wait_until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def opened(monkeypatch):
    """Record browser opens instead of really opening one; pretend the server is up."""
    urls = []
    monkeypatch.setattr(cli, "open_browser", urls.append)
    monkeypatch.setattr(cli, "wait_for_url", lambda url, **kw: True)
    return urls


def test_questions_are_answered_from_the_prompt_and_a_blank_line_is_an_answer(opened):
    out = io.StringIO()
    s = real_engine_session(out)
    s.handle_line("a todo app")
    assert wait_until(lambda: s.awaiting_answer)  # "Web or desktop?"
    s.handle_line("web")
    assert wait_until(lambda: s.awaiting_answer)  # where should it go?
    s.handle_line("")  # Enter: a new folder here
    assert wait_until(lambda: s.awaiting_answer)  # confirm the brief
    s.handle_line("y")
    s.thread.join(timeout=20)
    text = out.getvalue()
    assert "brief_ready" in text
    assert "Web or desktop? -> web" in text
    assert "Built in" in text
    assert s.project and s.project.endswith("a-todo-app")


def test_skip_command_skips_the_questions(opened):
    out = io.StringIO()
    s = real_engine_session(out)
    s.handle_line("a todo app")
    assert wait_until(lambda: s.awaiting_answer)
    s.handle_line("/skip")
    s.thread.join(timeout=20)
    text = out.getvalue()
    assert "brief_ready" not in text
    assert "success" in text


def test_skip_with_no_question_open():
    out = io.StringIO()
    s = fake_session(out)
    s.handle_line("/skip")
    assert "Nothing to skip" in out.getvalue()


def test_a_ui_project_is_launched_and_opened_after_the_build(opened):
    out = io.StringIO()
    s = real_engine_session(out)
    s.handle_line("a todo app")
    assert wait_until(lambda: s.awaiting_answer)
    s.handle_line("/skip")
    s.thread.join(timeout=20)
    assert wait_until(lambda: opened)
    assert opened == ["http://127.0.0.1:5000"]
    assert "Started: python app.py" in out.getvalue()


def test_approvals_on_means_no_automatic_launch(opened):
    out = io.StringIO()
    s = real_engine_session(out, approve=True)
    s.handle_line("a todo app")
    assert wait_until(lambda: s.awaiting_answer)
    s.handle_line("/skip")
    deadline = time.monotonic() + 20
    while s.running and time.monotonic() < deadline:
        if s.awaiting_approval:
            s.handle_line("y")
        time.sleep(0.02)
    assert "was not launched" in out.getvalue()
    assert opened == []


def test_resume_and_pause_say_so_when_they_do_nothing():
    out = io.StringIO()
    s = fake_session(out, delay=0.2)
    s.handle_line("build something")
    s.handle_line("/resume")
    assert "Not paused" in out.getvalue()
    s.handle_line("/pause")
    assert wait_until(lambda: s.paused)
    s.handle_line("/pause")
    assert "Already paused" in out.getvalue()
    s.handle_line("/resume")
    s.thread.join(timeout=10)
    assert "loop_done" in out.getvalue()


def test_a_bare_slash_does_not_crash():
    out = io.StringIO()
    s = fake_session(out)
    s.handle_line("/")
    assert "Try /help" in out.getvalue()


def make_project(tmp_path, run):
    project = tmp_path / "proj"
    (project / ".loop").mkdir(parents=True)
    (project / ".loop" / "run.json").write_text(json.dumps(run))
    (project / "app.py").write_text("import time\nprint('hello from app', flush=True)\ntime.sleep(60)\n")
    return str(project)


def test_run_streams_output_and_run_stop_ends_it(tmp_path):
    out = io.StringIO()
    s = fake_session(out)
    s.project = make_project(tmp_path, {"kind": "cli", "command": "python app.py"})
    s.handle_line("/run")
    assert wait_until(lambda: "hello from app" in out.getvalue())
    s.handle_line("/run")
    assert "Already running" in out.getvalue()
    s.handle_line("/run stop")
    assert "Stopped the built project" in out.getvalue()
    assert not s.app.running


def test_browser_needs_a_running_web_app(tmp_path, opened):
    out = io.StringIO()
    s = fake_session(out)
    s.project = make_project(tmp_path, {"kind": "web", "command": "python app.py", "url": "http://127.0.0.1:5000"})
    s.handle_line("/browser")
    assert "Start it with /run first" in out.getvalue()
    s.handle_line("/run")
    assert wait_until(lambda: s.app and s.app.running)
    s.handle_line("/browser")
    assert opened[-1] == "http://127.0.0.1:5000"
    s.handle_line("/run stop")


def test_run_open_and_push_without_a_project():
    out = io.StringIO()
    s = fake_session(out)
    for command in ("/run", "/open", "/push"):
        s.handle_line(command)
    assert out.getvalue().count("No project yet") == 3


def test_open_and_push_with_a_project(tmp_path, monkeypatch):
    out = io.StringIO()
    s = fake_session(out)
    s.project = str(tmp_path)
    opened_paths, pushed = [], []
    monkeypatch.setattr(cli, "open_in_editor", opened_paths.append)
    monkeypatch.setattr(cli, "push", pushed.append)
    s.handle_line("/open")
    s.handle_line("/push")
    assert wait_until(lambda: "Pushed." in out.getvalue())
    assert opened_paths == [str(tmp_path)]
    assert pushed == [str(tmp_path)]


def test_push_failure_is_reported(tmp_path, monkeypatch):
    out = io.StringIO()
    s = fake_session(out)
    s.project = str(tmp_path)

    def boom(project):
        raise RuntimeError("no remote")

    monkeypatch.setattr(cli, "push", boom)
    s.handle_line("/push")
    assert wait_until(lambda: "Push failed" in out.getvalue())


def test_an_engine_crash_keeps_the_session_open_and_saves_details(tmp_path):
    def exploding(role, text):
        raise RuntimeError("model exploded")

    out = io.StringIO()
    s = Session(
        Console(file=out, width=120),
        engine_factory=lambda ch, ask: engine_for(ch, ask, model_fn=exploding, intake=False),
    )
    s.handle_line("a todo app")
    s.thread.join(timeout=10)
    text = out.getvalue()
    assert "RuntimeError: model exploded" in text
    assert "error.txt" in text
    assert not s.should_exit
    s.handle_line("/status")  # the session still answers
    assert "Idle" in out.getvalue()
