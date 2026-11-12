from loop_engine.cli import main


def test_main_returns_cleanly():
    # Phase 1 done-when: main() runs and returns without error.
    assert main() is None
