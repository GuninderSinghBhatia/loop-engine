import json
import subprocess
import sys

import pytest

from loop_engine.control import ControlChannel
from loop_engine.engine import Engine

FENCE = "`" * 3


def fenced(lang, body, path=None):
    tag = f"{lang} path={path}" if path else lang
    return f"{FENCE}{tag}\n{body}\n{FENCE}"


SPEC = fenced("markdown", "# spec")
PLAN = SPEC + "\n" + fenced("python", "def test_x():\n    pass", path="test_app.py")
SPLIT = fenced(
    "json",
    json.dumps(
        {
            "tasks": [
                {"id": "a", "title": "Task A", "files": ["a.py"], "depends_on": [], "brief": "write a"},
                {"id": "b", "title": "Task B", "files": ["b.py"], "depends_on": [], "brief": "write b"},
                {"id": "c", "title": "Task C", "files": ["c.py"], "depends_on": ["a", "b"], "brief": "write c"},
            ],
            "run": {"kind": "web", "command": "python c.py", "url": "http://127.0.0.1:5000"},
        }
    ),
)

PASS = {"passed": True, "output": "3 passed in 0.01s", "timed_out": False}
RUFF_OK = {"passed": True, "output": "", "timed_out": False}


def fail(where="b.py"):
    return {
        "passed": False,
        "output": f"FAILED test_app.py::test_x - assert in {where}\n1 failed in 0.01s",
        "timed_out": False,
    }


class Model:
    """A scripted model that records every call."""

    def __init__(self, intake="NONE", plan=PLAN, split=SPLIT, builder_error=None):
        self.intake, self.plan, self.split, self.builder_error = intake, plan, split, builder_error
        self.calls = []

    def __call__(self, role, text):
        self.calls.append((role, text))
        if role == "intake":
            return self.intake
        if role == "planner":
            return self.plan
        if role == "splitter":
            return self.split
        if self.builder_error and role == "builder":
            raise self.builder_error
        # builder and fixer: write the file(s) named in "Files to write" / "Files you own"
        line = next(ln for ln in text.splitlines() if ln.startswith(("Files to write:", "Files you own:")))
        paths = [p.strip() for p in line.split(":", 1)[1].split(",")]
        return "\n".join(fenced("python", f"x = '{role}'", path=p) for p in paths)

    def of(self, role):
        return [t for r, t in self.calls if r == role]


def make(model, results=(PASS,), channel=None, **kw):
    results = iter(results)
    return Engine(
        channel or ControlChannel(),
        model=model,
        check_ruff=lambda ws: RUFF_OK,
        check_pytest=lambda ws, py=None: next(results),
        prepare_env=lambda ws: sys.executable,
        **kw,
    )


def drive(engine, request, answers=()):
    """Run to the end, answering each question in turn. "<skip>" sends /skip."""
    answers = iter(answers)
    events = []
    for event in engine.run(request):
        events.append(event)
        if event.kind == "intake_question":
            answer = next(answers)
            if answer == "<skip>":
                engine.channel.send("skip")
            else:
                engine.channel.send("answer", answer)
    return events


def kinds(events):
    return [e.kind for e in events]


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_intake_questions_and_confirmed_brief_reach_the_planner(tmp_path):
    model = Model(intake="Sure:\n1. Web or desktop?\n2. Any constraints?")
    events = drive(make(model, intake=True), "a todo app", ["web", "no login", "", "y"])
    assert kinds(events).count("intake_question") == 4  # 2 questions, location, confirm
    assert "brief_ready" in kinds(events)
    planner_prompt = model.of("planner")[0]
    assert "Web or desktop? -> web" in planner_prompt
    assert "a todo app" in planner_prompt
    repo = next(e.text for e in events if e.kind == "repo_ready")
    assert repo == str(tmp_path / "a-todo-app")  # a new folder named from the request
    assert events[-1].text.startswith("success")


def test_skip_starts_immediately_with_the_prompt_alone():
    model = Model(intake="1. Web or desktop?\n2. Any constraints?")
    events = drive(make(model, intake=True), "a todo app", ["<skip>"])
    assert kinds(events).count("intake_question") == 1
    assert "brief_ready" not in kinds(events)
    assert events[-1].text.startswith("success")


def test_rejecting_the_brief_stops_before_planning():
    model = Model(intake="1. Web or desktop?")
    events = drive(make(model, intake=True), "a todo app", ["web", "", "n"])
    assert events[-1].text.startswith("stopped: brief rejected")
    assert model.of("planner") == []


def test_stop_while_a_question_is_open():
    model = Model(intake="1. Web or desktop?")
    engine = make(model, intake=True)
    events = []
    for event in engine.run("a todo app"):
        events.append(event)
        if event.kind == "intake_question":
            engine.channel.send("stop")
    assert events[-1].text == "stopped"


def test_github_url_creates_a_local_folder_with_origin(tmp_path):
    url = "https://github.com/someone/cool-app.git"
    events = drive(make(Model(), intake=True), "a todo app", [url, "y"])
    assert next(e.text for e in events if e.kind == "remote_set") == url
    repo = tmp_path / "cool-app"
    assert (repo / ".git").is_dir()
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert origin.stdout.strip() == url


def test_a_non_empty_folder_is_refused(tmp_path):
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "keep.txt").write_text("mine")
    events = drive(make(Model(), intake=True), "a todo app", ["full", "y"])
    assert any(e.kind == "error" and "not empty" in e.text for e in events)
    assert events[-1].text == "failed: bad project location"
    assert (tmp_path / "full" / "keep.txt").read_text() == "mine"


def test_multi_task_build_writes_every_file_and_the_run_info(tmp_path):
    model = Model()
    events = drive(make(model), "a web app")
    repo = tmp_path / "a-web-app"
    for name in ("a.py", "b.py", "c.py", "test_app.py"):
        assert (repo / name).exists(), name
    assert (repo / ".loop" / "spec.md").exists()
    assert json.loads((repo / ".loop" / "run.json").read_text())["kind"] == "web"
    assert next(e.text for e in events if e.kind == "tasks_created").startswith("3 task(s)")
    assert kinds(events).count("agent_done") == 3
    assert events[-1].text.startswith("success")
    # each builder saw its own brief and the tests, not the whole spec
    task_c = next(t for t in model.of("builder") if "Task: Task C" in t)
    assert "write c" in task_c and "x = 'builder'" in task_c  # it saw its dependencies' files


def test_builder_cannot_overwrite_the_planners_tests(tmp_path):
    class Sneaky(Model):
        def __call__(self, role, text):
            reply = super().__call__(role, text)
            if role == "builder" and "Task: Task A" in text:
                reply += "\n" + fenced("python", "hacked", path="test_app.py")
            return reply

    events = drive(make(Sneaky()), "a web app")
    assert any(e.kind == "conflict" and "protected" in e.text for e in events)
    assert "hacked" not in (tmp_path / "a-web-app" / "test_app.py").read_text()


def test_only_the_affected_task_is_sent_to_the_fixer():
    model = Model()
    events = drive(make(model, results=[fail("b.py"), PASS]), "a web app")
    fixes = model.of("fixer")
    assert len(fixes) == 1
    assert "Files you own: b.py" in fixes[0]
    assert events[-1].text == "success: tests pass after 2 round(s)"


def test_unlocatable_failure_goes_to_every_task():
    model = Model()
    drive(make(model, results=[fail("somewhere else"), PASS]), "a web app")
    assert len(model.of("fixer")) == 3


def test_stuck_asks_the_user_and_retry_continues():
    model = Model()
    engine = make(model, results=[fail(), fail(), fail(), PASS], ask_when_stuck=True)
    events = drive(engine, "a web app", ["retry"])
    assert "stuck" in kinds(events)
    assert events[-1].text == "success: tests pass after 4 round(s)"


def test_stuck_then_stop_ends_the_run():
    model = Model()
    engine = make(model, results=[fail(), fail(), fail()], ask_when_stuck=True)
    events = drive(engine, "a web app", ["stop"])
    assert events[-1].text.startswith("failed: stuck")


def test_guidance_typed_when_stuck_reaches_the_fixer():
    model = Model()
    engine = make(model, results=[fail(), fail(), fail(), PASS], ask_when_stuck=True)
    drive(engine, "a web app", ["use a dict instead"])
    assert "use a dict instead" in model.of("fixer")[-1]


def test_a_crash_is_saved_to_the_loop_folder(tmp_path):
    engine = make(Model(builder_error=RuntimeError("model exploded")))
    with pytest.raises(RuntimeError, match="exploded"):
        drive(engine, "a web app")
    assert "model exploded" in (tmp_path / "a-web-app" / ".loop" / "error.txt").read_text()


def test_explicit_workspace_skips_the_location_question(tmp_path):
    target = tmp_path / "chosen"
    events = drive(make(Model(), workspace=str(target), intake=True), "a todo app", ["y"])
    assert next(e.text for e in events if e.kind == "repo_ready") == str(target)
    assert (target / "a.py").exists()
