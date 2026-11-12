import io
import time

from rich.console import Console

from loop_engine.cli import Session


def make_session(delay=0.0):
    out = io.StringIO()
    return Session(Console(file=out, width=100), fake=True, delay=delay), out


def test_idle_commands_say_nothing_is_running():
    s, out = make_session()
    s.handle_line("/pause")
    assert "Nothing is running" in out.getvalue()


def test_prompt_starts_a_run_to_the_end():
    s, out = make_session()
    s.handle_line("build a todo app")
    s.thread.join(timeout=5)
    text = out.getvalue()
    assert "phase_started" in text
    assert "loop_done" in text


def test_typing_during_a_run_steers_and_is_shown_literally():
    s, out = make_session(delay=0.05)
    s.handle_line("build a todo app")
    s.handle_line("[red]make it red")
    s.thread.join(timeout=5)
    assert "[red]make it red" in out.getvalue()


def test_stop_ends_the_run_early():
    s, out = make_session(delay=0.05)
    s.handle_line("build a todo app")
    s.handle_line("/stop")
    s.thread.join(timeout=5)
    text = out.getvalue()
    assert "stopped" in text
    assert "test_result" not in text


def test_unknown_command_is_reported():
    s, out = make_session()
    s.handle_line("/nope")
    assert "Unknown command" in out.getvalue()


def answer_all(s, answer, timeout=5):
    """Answer approval requests until the run ends."""
    end = time.time() + timeout
    while s.thread.is_alive() and time.time() < end:
        if s.awaiting_approval:
            s.handle_line(answer)
        time.sleep(0.01)


def test_approve_command_toggles_and_reports():
    s, out = make_session()
    s.handle_line("/approve on")
    assert s.approve is True
    s.handle_line("/approve off")
    assert s.approve is False
    assert "Approval is off" in out.getvalue()


def test_approval_no_skips_steps():
    s, out = make_session()
    s.handle_line("/approve on")
    s.handle_line("build a todo app")
    answer_all(s, "n")
    text = out.getvalue()
    assert "approval_needed" in text
    assert "step_skipped" in text
    assert "loop_done" in text


def test_approval_yes_runs_steps():
    s, out = make_session()
    s.handle_line("/approve on")
    s.handle_line("build a todo app")
    answer_all(s, "y")
    text = out.getvalue()
    assert "file_written" in text
    assert "step_skipped" not in text
