"""Second round of regressions from the live run: split parsing, ruff scope, fixer scope."""

import json
import sys

import pytest

from loop_engine.control import ControlChannel
from loop_engine.engine import Engine
from loop_engine.pipeline import guess_files, is_default_split, parse_split
from loop_engine.runner import run_ruff

FENCE = "`" * 3


def block(header, body):
    return f"{FENCE}{header}\n{body}\n{FENCE}"


def test_a_brief_containing_code_fences_does_not_break_the_split():
    brief = "Write app.py.\n" + block("html", "<h1>{{ counter }}</h1>") + "\nThen run it."
    reply = block(
        "json",
        json.dumps(
            {
                "tasks": [{"id": "app", "title": "App", "files": ["app.py"], "depends_on": [], "brief": brief}],
                "run": {"kind": "web", "command": "python app.py", "url": "http://127.0.0.1:5057"},
            }
        ),
    )
    tasks, run = parse_split(reply)
    assert [t["id"] for t in tasks] == ["app"]
    assert FENCE in tasks[0]["brief"]
    assert run["url"] == "http://127.0.0.1:5057"


def test_json_without_a_fence_and_with_trailing_chatter_is_still_read():
    reply = 'Here you go: {"tasks":[{"id":"a","files":["a.py"],"brief":"x"}]} hope it helps'
    tasks, _ = parse_split(reply)
    assert tasks[0]["files"] == ["a.py"]


def test_guess_files_reads_source_files_from_the_spec_but_not_tests():
    spec = "- **app.py** serves it\n- templates/index.html is the page\n- test_app.py checks it\n- app.py again"
    assert guess_files(spec) == ["app.py", "templates/index.html"]
    assert guess_files("nothing named here") == []


def test_default_split_is_recognised():
    tasks, _ = parse_split("not json at all")
    assert is_default_split(tasks)


def test_ruff_skips_test_files_and_ignores_project_config(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "test_app.py").write_text("import os\n")  # F401, but the planner's file
    assert run_ruff(str(tmp_path))["passed"] is True
    (tmp_path / "ruff.toml").write_text('[lint]\nignore = ["F401"]\n')
    (tmp_path / "bad.py").write_text("import os\n")  # a real F401 in a source file
    result = run_ruff(str(tmp_path))
    assert result["passed"] is False  # the config file cannot silence it
    assert "bad.py" in result["output"]


PLAN = (
    block("markdown", "# spec\n**app.py** serves a page")
    + "\n"
    + block("python path=test_app.py", "def test_x():\n    pass")
)
PASS = {"passed": True, "output": "1 passed in 0.01s", "timed_out": False}
FAIL = {
    "passed": False,
    "output": "FAILED test_app.py::test_x - assert in app.py\n1 failed in 0.01s",
    "timed_out": False,
}


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def make_engine(model, results):
    results = iter(results)
    return Engine(
        ControlChannel(),
        model=model,
        check_ruff=lambda ws: {"passed": True, "output": "", "timed_out": False},
        check_pytest=lambda ws, py=None: next(results),
        prepare_env=lambda ws: sys.executable,
    )


def test_an_unreadable_split_builds_the_files_the_spec_names(tmp_path):
    def model(role, text):
        if role == "planner":
            return PLAN
        if role == "splitter":
            return "I could not produce JSON, sorry"
        return block("python", "x = 1")  # a plain block: accepted for a single file

    events = list(make_engine(model, [PASS]).run("a tiny app"))
    assert events[-1].text.startswith("success")
    assert (tmp_path / "a-tiny-app" / "app.py").exists()  # not solution.py
    assert not (tmp_path / "a-tiny-app" / "solution.py").exists()


def test_the_fixer_cannot_add_config_files_or_touch_other_tasks(tmp_path):
    split = block(
        "json",
        json.dumps({"tasks": [{"id": "a", "title": "A", "files": ["app.py"], "depends_on": [], "brief": "b"}]}),
    )

    def model(role, text):
        if role == "planner":
            return PLAN
        if role == "splitter":
            return split
        if role == "fixer":
            return (
                block("python path=app.py", "x = 2")
                + block("toml path=ruff.toml", '[lint]\nignore = ["F401"]')
                + block("python path=other.py", "y = 1")
            )
        return block("python path=app.py", "x = 1")

    events = list(make_engine(model, [FAIL, PASS]).run("a tiny app"))
    project = tmp_path / "a-tiny-app"
    assert events[-1].text.startswith("success")
    assert (project / "app.py").read_text() == "x = 2\n"
    assert not (project / "ruff.toml").exists()
    assert not (project / "other.py").exists()
    assert sum(1 for e in events if e.kind == "conflict" and "ignored" in e.text) == 2
