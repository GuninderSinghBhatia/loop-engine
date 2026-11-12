import pytest

from loop_engine.control import ControlChannel
from loop_engine.engine import Engine

FENCE = "`" * 3
PLAN = f"{FENCE}markdown\n# spec\n{FENCE}\n{FENCE}python\ndef test_x():\n    pass\n{FENCE}"
CODE = f"{FENCE}python\nx = 1\n{FENCE}"

PASS = {"passed": True, "output": "1 passed in 0.01s", "timed_out": False}
FAIL = {
    "passed": False,
    "output": "FAILED test_solution.py::test_a - assert 1 == 2\n1 failed in 0.01s",
    "timed_out": False,
}
RUFF_OK = {"passed": True, "output": "", "timed_out": False}


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the engine writes workspace/ and runs git here


class FakeModel:
    def __init__(self):
        self.calls = []  # (role, prompt)

    def __call__(self, role, text):
        self.calls.append((role, text))
        return PLAN if role == "planner" else CODE

    def roles(self):
        return [r for r, _ in self.calls]


def make_engine(pytest_results, model=None, channel=None, **kw):
    results = iter(pytest_results)
    return Engine(
        channel or ControlChannel(),
        model=model or FakeModel(),
        check_ruff=lambda ws: RUFF_OK,
        check_pytest=lambda ws: next(results),
        **kw,
    )


def kinds(events):
    return [e.kind for e in events]


def test_success_on_first_round():
    events = list(make_engine([PASS]).run("a tiny project"))
    assert kinds(events)[0] == "phase_started"
    assert "spec_written" in kinds(events)
    assert "file_written" in kinds(events)
    assert events[-1].kind == "loop_done"
    assert events[-1].text.startswith("success")


def test_failure_is_routed_to_the_fixer_then_passes():
    model = FakeModel()
    events = list(make_engine([FAIL, PASS], model=model).run("a tiny project"))
    assert model.roles() == ["planner", "builder", "fixer"]
    assert events[-1].text == "success: tests pass after 2 round(s)"


def test_same_failure_three_times_is_stuck():
    events = list(make_engine([FAIL, FAIL, FAIL, FAIL]).run("a tiny project"))
    assert events[-1].kind == "loop_done"
    assert "stuck" in events[-1].text


def test_ruff_failure_skips_pytest():
    ruff_bad = {"passed": False, "output": "F401 unused import", "timed_out": False}
    calls = []
    engine = Engine(
        ControlChannel(),
        model=FakeModel(),
        check_ruff=lambda ws: ruff_bad,
        check_pytest=lambda ws: calls.append(1) or PASS,
        max_rounds=1,
    )
    events = list(engine.run("a tiny project"))
    assert calls == []
    assert any(e.kind == "test_result" and "ruff" in e.text for e in events)


def test_stop_before_start_never_calls_the_model():
    ch = ControlChannel()
    ch.send("stop")
    model = FakeModel()
    events = list(make_engine([PASS], model=model, channel=ch).run("a tiny project"))
    assert model.calls == []
    assert events[-1].text == "stopped"


def test_steering_reaches_the_next_prompt():
    ch = ControlChannel()
    ch.send("steer", "use a dataclass")
    model = FakeModel()
    events = list(make_engine([PASS], model=model, channel=ch).run("a tiny project"))
    assert "steer_received" in kinds(events)
    assert all("use a dataclass" in text for _, text in model.calls)


def test_declining_approval_ends_the_run():
    ch = ControlChannel()
    engine = make_engine([PASS], channel=ch, ask_approval=lambda: True)
    events = []
    for e in engine.run("a tiny project"):
        events.append(e)
        if e.kind == "approval_needed":
            ch.send("approval", "no")
    assert "approval_needed" in kinds(events)
    assert "file_written" not in kinds(events)
    assert events[-1].text == "stopped: approval declined"


def test_approval_yes_lets_the_run_finish():
    ch = ControlChannel()
    engine = make_engine([PASS], channel=ch, ask_approval=lambda: True)
    events = []
    for e in engine.run("a tiny project"):
        events.append(e)
        if e.kind == "approval_needed":
            ch.send("approval", "yes")
    assert events[-1].text.startswith("success")
