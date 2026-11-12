"""Regressions found by the first live run against a real model."""

import json
import sys

import pytest

from loop_engine.control import ControlChannel
from loop_engine.engine import Engine
from loop_engine.pipeline import extract_spec, files_from_reply, parse_file_blocks

FENCE = "`" * 3


def block(header, body):
    return f"{FENCE}{header}\n{body}\n{FENCE}"


@pytest.mark.parametrize(
    "header",
    [
        "python path=app.py",
        "python app.py",  # what the real builder model wrote
        'python filename="app.py"',
        "python file: app.py",
        "py path='app.py'",
    ],
)
def test_file_block_headers_models_actually_write(header):
    assert parse_file_blocks(block(header, "x = 1")) == {"app.py": "x = 1\n"}


def test_nested_folders_and_non_python_files():
    reply = block("html templates/index.html", "<p>hi</p>") + block("text requirements.txt", "flask")
    assert parse_file_blocks(reply) == {"templates/index.html": "<p>hi</p>\n", "requirements.txt": "flask\n"}


def test_a_plain_python_block_is_not_mistaken_for_a_file_block():
    assert parse_file_blocks(block("python", "x = 1")) == {}


def test_spec_keeps_its_own_code_fences():
    spec_body = "# Spec\n## Interface\n" + block("python", "def add(a, b): ...") + "\nMore rules."
    reply = block("markdown", spec_body) + "\n" + block("python path=test_app.py", "def test_x(): pass")
    spec = extract_spec(reply)
    assert "def add(a, b)" in spec
    assert spec.endswith("More rules.")
    assert FENCE + "\n" not in spec.splitlines()[-1]  # no dangling closing fence


def test_spec_with_a_plain_python_test_block_uses_the_last_python_block_as_the_tests():
    reply = block("markdown", "# spec\n" + block("python", "example = 1")) + "\n" + block("python", "def test_x(): pass")
    assert "example = 1" in extract_spec(reply)


def test_files_from_reply_rejects_garbage():
    with pytest.raises(ValueError):
        files_from_reply("Sure! Here is the code you asked for.", "app.py")


PLAN = (
    block("markdown", "# spec\n" + block("python", "def add(a, b): ...") + "\nend")
    + "\n"
    + block("python path=test_app.py", "def test_x():\n    pass")
)
SPLIT = block(
    "json",
    json.dumps({"tasks": [{"id": "a", "title": "A", "files": ["app.py"], "depends_on": [], "brief": "b"}]}),
)
PASS = {"passed": True, "output": "1 passed in 0.01s", "timed_out": False}


def make_engine(builder_replies):
    replies = iter(builder_replies)
    calls = []

    def model(role, text):
        calls.append((role, text))
        if role == "planner":
            return PLAN
        if role == "splitter":
            return SPLIT
        return next(replies)

    engine = Engine(
        ControlChannel(),
        model=model,
        check_ruff=lambda ws: {"passed": True, "output": "", "timed_out": False},
        check_pytest=lambda ws, py=None: PASS,
        prepare_env=lambda ws: sys.executable,
    )
    return engine, calls


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_a_malformed_builder_reply_gets_one_corrective_retry(tmp_path):
    engine, calls = make_engine(["Sure! I would write a counter app.", block("python app.py", "x = 1")])
    events = list(engine.run("a tiny app"))
    assert events[-1].text.startswith("success")
    builder_calls = [t for r, t in calls if r == "builder"]
    assert len(builder_calls) == 2
    assert "Format error" in builder_calls[1]
    assert list((tmp_path / "a-tiny-app" / ".loop").glob("bad_reply_builder_*.txt"))  # kept for diagnosis
    assert "def add(a, b)" in (tmp_path / "a-tiny-app" / ".loop" / "spec.md").read_text()  # spec not truncated


def test_two_malformed_replies_in_a_row_surface_as_an_error(tmp_path):
    engine, _ = make_engine(["nope", "still nope"])
    with pytest.raises(ValueError, match="no file blocks"):
        list(engine.run("a tiny app"))
    assert (tmp_path / "a-tiny-app" / ".loop" / "error.txt").exists()
