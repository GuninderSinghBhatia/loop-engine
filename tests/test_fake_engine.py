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
