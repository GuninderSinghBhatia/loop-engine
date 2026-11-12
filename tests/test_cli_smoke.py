from loop_engine.cli import main


def test_main_is_the_entry_point():
    # main() is interactive now, so we only check the entry point exists.
    # Behaviour is tested in test_session.py.
    assert callable(main)
