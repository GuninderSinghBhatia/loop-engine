import threading

import pytest

from loop_engine.pipeline import (
    affected_tasks,
    build_graph,
    collect_files,
    files_from_reply,
    parse_file_blocks,
    parse_split,
    safe_relpath,
)

FENCE = "`" * 3


def block(lang, path, body):
    return f"{FENCE}{lang} path={path}\n{body}\n{FENCE}"


def test_parses_path_tagged_blocks():
    reply = block("python", "app.py", "x = 1") + "\n" + block("html", "templates/index.html", "<p>hi</p>")
    assert parse_file_blocks(reply) == {"app.py": "x = 1\n", "templates/index.html": "<p>hi</p>\n"}


@pytest.mark.parametrize("bad", ["../evil.py", "/etc/passwd", "C:/x.py", ".git/config", ".loop/log.md", "a//b.py", ""])
def test_unsafe_paths_are_dropped(bad):
    assert safe_relpath(bad) is None
    assert parse_file_blocks(block("python", bad or " ", "x = 1")) == {}


def test_plain_block_needs_a_single_fallback_file():
    reply = f"{FENCE}python\nx = 1\n{FENCE}"
    assert files_from_reply(reply, "solution.py") == {"solution.py": "x = 1\n"}
    with pytest.raises(ValueError):
        files_from_reply(reply, None)


def split_reply(body):
    return f"{FENCE}json\n{body}\n{FENCE}"


def test_parse_split_reads_tasks_and_run_info():
    reply = split_reply(
        '{"tasks":[{"id":"a","title":"A","files":["a.py"],"depends_on":[],"brief":"do a"},'
        '{"id":"b","title":"B","files":["b.py"],"depends_on":["a"],"brief":"do b"}],'
        '"run":{"kind":"web","command":"python b.py","url":"http://127.0.0.1:5000"}}'
    )
    tasks, run = parse_split(reply)
    assert [t["id"] for t in tasks] == ["a", "b"]
    assert tasks[1]["depends_on"] == ["a"]
    assert run["kind"] == "web"


@pytest.mark.parametrize(
    "reply",
    [
        "no json here",
        split_reply("{not json"),
        split_reply('{"tasks": []}'),
        split_reply('{"tasks":[{"id":"a","files":["a.py"],"depends_on":["ghost"]}]}'),
        split_reply(
            '{"tasks":[{"id":"a","files":["a.py"],"depends_on":["b"]},'
            '{"id":"b","files":["b.py"],"depends_on":["a"]}]}'
        ),
    ],
)
def test_bad_split_falls_back_to_one_task(reply):
    tasks, run = parse_split(reply)
    assert [t["files"] for t in tasks] == [["solution.py"]]
    assert run is None


def test_collect_reports_conflicts_and_protects_tests():
    tasks = [
        {"id": "a", "files": ["a.py", "shared.py"]},
        {"id": "b", "files": ["b.py"]},
    ]
    outputs = {
        "a": {"a.py": "A", "shared.py": "from a"},
        "b": {"b.py": "B", "shared.py": "from b", "test_x.py": "hacked"},
    }
    files, conflicts = collect_files(tasks, outputs, protected={"test_x.py"})
    assert files == {"a.py": "A", "shared.py": "from a", "b.py": "B"}
    assert len(conflicts) == 2
    assert any("protected" in c for c in conflicts)
    assert any("shared.py" in c and "kept a" in c for c in conflicts)


def test_affected_tasks_match_by_file_name_or_fall_back_to_all():
    tasks = [{"id": "a", "files": ["models.py"]}, {"id": "b", "files": ["views/app.py"]}]
    assert [t["id"] for t in affected_tasks(tasks, "FAILED test_x - in views\\app.py line 3")] == ["b"]
    assert len(affected_tasks(tasks, "something unrelated")) == 2


def test_graph_runs_independent_tasks_in_parallel_and_respects_dependencies():
    started = []
    lock = threading.Lock()
    both_running = threading.Event()

    def run_task(task, context):
        with lock:
            started.append(task["id"])
            if {"a", "b"} <= set(started):
                both_running.set()
        if task["id"] in ("a", "b"):
            assert both_running.wait(timeout=5), "a and b should be running at the same time"
        if task["id"] == "c":
            assert set(context) == {"a.py", "b.py"}  # c sees what its dependencies wrote
        return {f"{task['id']}.py": task["id"]}

    tasks = [
        {"id": "a", "title": "A", "files": ["a.py"], "depends_on": []},
        {"id": "b", "title": "B", "files": ["b.py"], "depends_on": []},
        {"id": "c", "title": "C", "files": ["c.py"], "depends_on": ["a", "b"]},
    ]
    graph = build_graph(run_task)
    events, final = [], None
    for mode, chunk in graph.stream({"tasks": tasks, "outputs": {}}, stream_mode=["custom", "values"]):
        if mode == "custom":
            events.append(chunk["kind"])
        else:
            final = chunk
    assert set(final["outputs"]) == {"a", "b", "c"}
    assert started[-1] == "c"
    assert events.count("agent_started") == 3
    assert events.count("agent_done") == 3


def test_graph_surfaces_an_agent_crash():
    def run_task(task, context):
        raise ValueError("model reply was garbage")

    graph = build_graph(run_task)
    tasks = [{"id": "a", "title": "A", "files": ["a.py"], "depends_on": []}]
    with pytest.raises(ValueError, match="garbage"):
        list(graph.stream({"tasks": tasks, "outputs": {}}, stream_mode="values"))
