from loop_engine.control import ControlChannel
from loop_engine.fake_engine import FakeEngine


def kinds(engine, prompt="build a todo app"):
    return [e.kind for e in engine.run(prompt)]


def test_full_run_starts_with_phase_and_ends_with_loop_done():
    ks = kinds(FakeEngine(ControlChannel()))
    assert ks[0] == "phase_started"
    assert ks[-1] == "loop_done"
    assert "test_result" in ks


def test_stop_ends_run_early():
    ch = ControlChannel()
    ch.send("stop")
    ks = kinds(FakeEngine(ch))
    assert ks[-1] == "loop_done"
    assert "test_result" not in ks


def test_steer_is_acknowledged():
    ch = ControlChannel()
    ch.send("steer", "make it red")
    events = list(FakeEngine(ch).run("build a todo app"))
    steer = [e for e in events if e.kind == "steer_received"]
    assert steer and steer[0].text == "make it red"


def run_with_answers(answer):
    """Run with approval on, answering every approval request with `answer`."""
    ch = ControlChannel()
    events = []
    for e in FakeEngine(ch, ask_approval=lambda: True).run("build a todo app"):
        events.append(e)
        if e.kind == "approval_needed":
            ch.send("approval", answer)
    return events


def test_approval_no_skips_the_step():
    events = run_with_answers("no")
    ks = [e.kind for e in events]
    assert "approval_needed" in ks
    assert "step_skipped" in ks
    assert "file_written" not in ks
    assert ks[-1] == "loop_done"


def test_approval_yes_runs_the_step():
    ks = [e.kind for e in run_with_answers("yes")]
    assert "file_written" in ks
    assert "step_skipped" not in ks


def test_stop_while_waiting_for_approval():
    ch = ControlChannel()
    events = []
    for e in FakeEngine(ch, ask_approval=lambda: True).run("x"):
        events.append(e)
        if e.kind == "approval_needed":
            ch.send("stop")
    assert events[-1].kind == "loop_done"
    assert events[-1].text == "stopped"
