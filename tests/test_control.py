from loop_engine.control import ControlChannel


def test_receive_returns_none_when_empty():
    assert ControlChannel().receive() is None


def test_steer_messages_keep_order():
    ch = ControlChannel()
    ch.send("steer", "make it red")
    ch.send("steer", "add a footer")
    assert ch.receive() == ("steer", "make it red")
    assert ch.receive() == ("steer", "add a footer")


def test_stop_jumps_ahead_of_steer():
    ch = ControlChannel()
    ch.send("steer", "make it red")
    ch.send("steer", "add a footer")
    ch.send("stop")
    assert ch.receive() == ("stop", None)
