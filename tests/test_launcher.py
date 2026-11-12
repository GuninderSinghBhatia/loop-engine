import http.server
import json
import threading
import time

from loop_engine.launcher import (
    AppProcess,
    RunSpec,
    build_command,
    load_run_spec,
    wait_for_url,
)


def write_run_json(workspace, data):
    (workspace / ".loop").mkdir()
    (workspace / ".loop" / "run.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_run_spec(tmp_path):
    write_run_json(tmp_path, {"kind": "web", "command": "python app.py", "url": "http://127.0.0.1:5000"})
    spec = load_run_spec(str(tmp_path))
    assert spec == RunSpec("web", "python app.py", "http://127.0.0.1:5000")
    assert spec.has_ui


def test_missing_or_broken_run_json_is_none(tmp_path):
    assert load_run_spec(str(tmp_path)) is None
    write_run_json(tmp_path, {"nope": 1})
    assert load_run_spec(str(tmp_path)) is None


def test_cli_and_library_projects_have_no_ui():
    assert not RunSpec("cli", "python main.py").has_ui
    assert not RunSpec("none").has_ui
    assert not RunSpec("web", None).has_ui  # a web app with no start command cannot launch


def test_build_command_handles_quoted_arguments():
    assert build_command('python -c "print(1)"', "/venv/python") == ["/venv/python", "-c", "print(1)"]


def test_build_command_swaps_the_interpreter():
    assert build_command("python app.py --port 5000", "/venv/python") == ["/venv/python", "app.py", "--port", "5000"]
    assert build_command("node server.js", "/venv/python") == ["node", "server.js"]


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_wait_for_url_sees_a_live_server_and_gives_up_on_a_dead_one():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert wait_for_url(f"http://127.0.0.1:{server.server_port}/", timeout=5)
    finally:
        server.shutdown()
    start = time.monotonic()
    assert not wait_for_url("http://127.0.0.1:9/", timeout=0.6, interval=0.1)
    assert time.monotonic() - start < 5


def test_app_process_streams_output_and_stops(tmp_path):
    (tmp_path / "app.py").write_text("import time\nprint('hello', flush=True)\ntime.sleep(60)\n")
    lines = []
    app = AppProcess(RunSpec("cli", "python app.py"), str(tmp_path), lines.append)
    app.start()
    deadline = time.monotonic() + 10
    while "hello" not in lines and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "hello" in lines
    assert app.running
    app.stop()
    assert not app.running
