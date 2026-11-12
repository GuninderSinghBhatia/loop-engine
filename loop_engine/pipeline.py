"""The multi-agent build stage: split the spec, run sub-agents in parallel, collect files.

Pure functions plus one LangGraph graph. The graph fans out one sub-agent per
ready task (a task is ready when the tasks it depends on are done), waits for
them, and repeats until every task has produced its files.
"""

import json
import re
from collections.abc import Callable
from typing import Annotated, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

MAX_PARALLEL = 4  # most sub-agents running at once

# A file block's header names the file. Models vary: "python path=app.py", "python app.py",
# 'python filename="app.py"'. All of them are accepted.
_PATH_BLOCK = re.compile(
    r"```[\w+.-]*[ \t]+(?:(?:path|file|filename)[=:][ \t]*)?[\"']?([\w./\\-]+\.\w+)[\"']?[ \t]*\n(.*?)```",
    re.DOTALL,
)
_PLAIN_BLOCK = re.compile(r"```python[ \t]*\n(.*?)```", re.DOTALL)
_RESERVED = (".git", ".loop", ".venv")


# ---- file blocks ------------------------------------------------------------


def safe_relpath(path: str) -> str | None:
    """A project-relative path, or None if it could escape the project or hit our own folders."""
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    parts = path.split("/")
    if ".." in parts or "" in parts or parts[0] in _RESERVED:
        return None
    return path


def parse_file_blocks(reply: str) -> dict[str, str]:
    """Every ```<lang> path=<file> block in the reply as {path: contents}. Unsafe paths are dropped."""
    files: dict[str, str] = {}
    for path, body in _PATH_BLOCK.findall(reply):
        safe = safe_relpath(path)
        if safe:
            files[safe] = body.strip("\n") + "\n"
    return files


def extract_spec(reply: str) -> str:
    """The planner's ```markdown spec.

    The spec may contain code fences of its own, so it is cut at the start of the first
    file block (or the last plain python block), not at the first inner fence.
    """
    start = re.search(r"```markdown[ \t]*\n", reply)
    if not start:
        raise ValueError("No markdown code block found in the reply")
    rest = reply[start.end():]
    cut = _PATH_BLOCK.search(rest)
    if cut:
        end = cut.start()
    else:
        plain = list(re.finditer(r"```python[ \t]*\n", rest))
        end = plain[-1].start() if plain else len(rest)
    body = rest[:end].rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


def files_from_reply(reply: str, fallback_path: str | None) -> dict[str, str]:
    """File blocks from a builder or fixer reply.

    A plain ```python block with no path is accepted only when there is exactly one
    file it could belong to (fallback_path); otherwise the model broke the format.
    """
    files = parse_file_blocks(reply)
    if files:
        return files
    plain = _PLAIN_BLOCK.findall(reply)
    if plain and fallback_path:
        return {fallback_path: plain[0].strip("\n") + "\n"}
    raise ValueError("The reply had no file blocks (expected ```python path=<file>)")


# ---- splitting the spec into tasks -------------------------------------------


def default_split(note: str = "") -> tuple[list[dict], dict | None]:
    """One task that builds everything into solution.py: the v1-style fallback."""
    task = {
        "id": "main",
        "title": "Implement the spec",
        "files": ["solution.py"],
        "depends_on": [],
        "brief": "Implement the whole spec." + (f" {note}" if note else ""),
    }
    return [task], None


def is_default_split(tasks: list[dict]) -> bool:
    return len(tasks) == 1 and tasks[0]["id"] == "main" and tasks[0]["files"] == ["solution.py"]


def guess_files(spec: str) -> list[str]:
    """Source files named in the spec (not tests), for when the split could not be read."""
    names = re.findall(r"[\w./-]+\.(?:py|html|css|js)\b", spec)
    seen: list[str] = []
    for name in names:
        safe = safe_relpath(name)
        base = (safe or "").rsplit("/", 1)[-1]
        if safe and not base.startswith("test_") and not base.endswith("_test.py") and safe not in seen:
            seen.append(safe)
    return seen


def _has_cycle(tasks: list[dict]) -> bool:
    done: set[str] = set()
    remaining = list(tasks)
    while remaining:
        ready = [t for t in remaining if set(t["depends_on"]) <= done]
        if not ready:
            return True
        done |= {t["id"] for t in ready}
        remaining = [t for t in remaining if t["id"] not in done]
    return False


def parse_split(reply: str) -> tuple[list[dict], dict | None]:
    """Read the splitter's ```json block into (tasks, run_info).

    Anything malformed falls back to a single task, so a bad split never stops a build.
    run_info is {"kind": "web|desktop|cli|none", "command": ..., "url": ...} or None.
    """
    # A brief may contain code fences of its own, so parse one JSON object instead of
    # cutting the reply at the first closing fence.
    fence = re.search(r"```json[ \t]*\n", reply)
    start = reply.find("{", fence.end() if fence else 0)
    if start < 0:
        return default_split()
    try:
        data, _ = json.JSONDecoder().raw_decode(reply[start:])
        raw_tasks = data["tasks"]
        tasks = []
        for raw in raw_tasks:
            files = [f for f in (safe_relpath(x) for x in raw["files"]) if f]
            if not files:
                raise ValueError("task without files")
            tasks.append(
                {
                    "id": str(raw["id"]),
                    "title": str(raw.get("title") or raw["id"]),
                    "files": files,
                    "depends_on": [str(d) for d in raw.get("depends_on", [])],
                    "brief": str(raw.get("brief", "")),
                }
            )
        ids = [t["id"] for t in tasks]
        if not tasks or len(set(ids)) != len(ids):
            raise ValueError("empty task list or duplicate ids")
        if any(d not in ids for t in tasks for d in t["depends_on"]) or _has_cycle(tasks):
            raise ValueError("bad dependencies")
        run_info = data.get("run")
        if not isinstance(run_info, dict) or run_info.get("kind") not in ("web", "desktop", "cli", "none"):
            run_info = None
        return tasks, run_info
    except (ValueError, KeyError, TypeError):
        return default_split()


# ---- collecting what the agents wrote ----------------------------------------


def collect_files(
    tasks: list[dict], outputs: dict[str, dict[str, str]], protected: set[str]
) -> tuple[dict[str, str], list[str]]:
    """Merge every task's files into one project, reporting conflicts.

    - A path written by two tasks goes to the task that declares it; if none does, the first.
    - A path in `protected` (the planner's tests and requirements) is never overwritten.
    """
    files: dict[str, str] = {}
    owner: dict[str, dict] = {}  # path -> the task that currently holds it
    conflicts: list[str] = []
    for task in tasks:
        for path, text in outputs.get(task["id"], {}).items():
            if path in protected:
                conflicts.append(f"{task['id']} tried to overwrite protected file {path}; ignored")
                continue
            if path in files:
                new_wins = path in task["files"] and path not in owner[path]["files"]
                kept = task if new_wins else owner[path]
                conflicts.append(
                    f"{path} written by both {owner[path]['id']} and {task['id']}; kept {kept['id']}"
                )
                if not new_wins:
                    continue
            files[path] = text
            owner[path] = task
    return files, conflicts


def affected_tasks(tasks: list[dict], failure_output: str) -> list[dict]:
    """Tasks whose files are named in a failure report; every task if none is named."""
    hit = [t for t in tasks if any(f in failure_output.replace("\\", "/") for f in t["files"])]
    return hit or tasks


# ---- the graph ---------------------------------------------------------------


def _merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class BuildState(TypedDict):
    tasks: list[dict]
    outputs: Annotated[dict[str, dict[str, str]], _merge]  # task id -> the files it wrote


def build_graph(run_task: Callable[[dict, dict[str, str]], dict[str, str]]):
    """Compile the fan-out graph. run_task(task, dependency_files) -> {path: text}."""

    def route(state: BuildState):
        done = set(state["outputs"])
        ready = [t for t in state["tasks"] if t["id"] not in done and set(t["depends_on"]) <= done]
        if not ready:
            if len(done) < len(state["tasks"]):
                raise RuntimeError("task dependencies cannot be satisfied")
            return END
        return [
            Send(
                "agent",
                {
                    "task": t,
                    "context": {
                        path: text for d in t["depends_on"] for path, text in state["outputs"][d].items()
                    },
                },
            )
            for t in ready
        ]

    def agent(payload: dict):
        task = payload["task"]
        write = get_stream_writer()
        write({"kind": "agent_started", "text": task["title"]})
        files = run_task(task, payload["context"])
        write({"kind": "agent_done", "text": f"{task['title']}: {', '.join(sorted(files))}"})
        return {"outputs": {task["id"]: files}}

    graph = StateGraph(BuildState)
    graph.add_node("gather", lambda state: {})
    graph.add_node("agent", agent)
    graph.add_edge(START, "gather")
    graph.add_conditional_edges("gather", route, ["agent", END])
    graph.add_edge("agent", "gather")
    return graph.compile()
