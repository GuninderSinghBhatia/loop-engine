"""The real engine: intake, spec, split, parallel build, check and fix. It emits events.

It never prints. At safe points between steps it reads the control channel
(pause, stop, steer, answers, approvals), so the CLI can steer a live run.
"""

import json
import os
import re
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from langchain_groq import ChatGroq

from loop_engine.config import MODELS
from loop_engine.control import ControlChannel
from loop_engine.env import ensure_env
from loop_engine.events import Event
from loop_engine.intake import format_brief, parse_questions
from loop_engine.pipeline import (
    MAX_PARALLEL,
    affected_tasks,
    build_graph,
    collect_files,
    extract_spec,
    files_from_reply,
    guess_files,
    is_default_split,
    parse_file_blocks,
    parse_split,
)
from loop_engine.project import resolve_location, slugify
from loop_engine.runner import run_pytest, run_ruff
from loop_engine.steering import SKIP, STOP, Steerable
from loop_engine.vcs import (
    add_remote,
    best_round,
    changed_summary,
    commit_round,
    init_repo,
    rewind_to,
)

ROLES_DIR = Path(__file__).parent / "roles"
# Load the key from the project's .env first, then from the current folder.
# Neither overrides a variable that is already set.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv()

MAX_ROUNDS = 7
STUCK_RETRY_ROUNDS = 3  # extra rounds granted each time the user says "retry"
MAX_STUCK_PROMPTS = 3  # then the run stops for good


# ---- pure helpers -----------------------------------------------------------


def first_block(reply: str, lang: str) -> str:
    """Return the first ```<lang> block, or raise if the model forgot it."""
    pattern = rf"```{re.escape(lang)}[ \t]*\n(.*?)```"
    blocks = [b.strip() for b in re.findall(pattern, reply, re.DOTALL)]
    if not blocks:
        raise ValueError(f"No {lang} code block found in the reply")
    return blocks[0]


def failure_signature(output: str, timed_out: bool = False) -> str:
    """Stable label for a pytest failure: failing test names, error types, or TIMEOUT."""
    if timed_out:
        return "TIMEOUT"
    tests = re.findall(r"FAILED \S+::(\S+)", output)
    errors = re.findall(r"^ERROR \S+ - (\w+)", output, re.MULTILINE)
    return ",".join(sorted(set(tests + errors))) or "UNKNOWN"


def ruff_signature(output: str) -> str:
    """Stable label for ruff output: sorted rule codes, no line numbers."""
    new_style = re.findall(r"^([A-Z]+\d+|invalid-syntax)\b", output, re.MULTILINE)
    old_style = re.findall(r":\d+:\d+: ([A-Z]+\d+)", output)
    codes = new_style + old_style
    return ",".join(sorted(set(codes))) or "UNKNOWN"


def count_passed(output: str) -> int:
    """Number of passed tests from pytest's summary line, or 0 if there isn't one."""
    # The summary is the last line ending in "in <seconds>s"; only look for "passed" there.
    summaries = re.findall(r"^.*\bin [\d.]+s.*$", output, re.MULTILINE)
    if not summaries:
        return 0
    counts = re.findall(r"(\d+) passed", summaries[-1])
    return int(counts[0]) if counts else 0


def format_files(files: dict[str, str]) -> str:
    """Files as prompt text: a heading and a fenced block per file."""
    fence = "`" * 3
    return "\n".join(f"### {path}\n{fence}\n{text.rstrip()}\n{fence}\n" for path, text in files.items())


def build_task_prompt(task: dict, context: dict[str, str], tests_text: str) -> str:
    """What one builder sees: its task, its dependencies' files and the tests. Not the whole spec."""
    parts = [
        f"## Task: {task['title']}",
        f"Files to write: {', '.join(task['files'])}",
        f"\n### Brief\n{task['brief']}",
    ]
    if context:
        parts.append(f"\n## Files from the tasks you depend on\n{format_files(context)}")
    parts.append(f"\n## Tests\n{tests_text}")
    return "\n".join(parts) + "\n"


def build_fixer_prompt(
    spec: str, tests_text: str, task: dict, current: dict[str, str], failure_output: str
) -> str:
    """What one fixer sees: the spec, the tests, its task and files, and the real failure."""
    return (
        f"## Spec\n{spec}\n\n"
        f"## Tests\n{tests_text}\n"
        f"## Task: {task['title']}\nFiles you own: {', '.join(task['files'])}\n\n"
        f"### Brief\n{task['brief']}\n\n"
        f"## Current files\n{format_files(current)}\n"
        f"## Failure output\n{failure_output[:4000]}\n"
    )


def call_role(role: str, user_text: str) -> str:
    """Call the model for one role: its prompt file is the system prompt."""
    system_prompt = (ROLES_DIR / f"{role}.md").read_text(encoding="utf-8")
    model = ChatGroq(model=MODELS[role], temperature=0)
    response = model.invoke([("system", system_prompt), ("human", user_text)])
    return response.content


def write_files(workspace: str, files: dict[str, str]) -> None:
    """Write project files, creating folders as needed."""
    for path, text in files.items():
        full = os.path.join(workspace, path)
        os.makedirs(os.path.dirname(full) or workspace, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)


def loop_dir(workspace: str) -> str:
    """The folder for the engine's own records: log, spec, run info, saved errors."""
    path = os.path.join(workspace, ".loop")
    os.makedirs(path, exist_ok=True)
    return path


def reset_log(workspace: str, request: str) -> None:
    """Start a fresh log.md for this run."""
    with open(os.path.join(loop_dir(workspace), "log.md"), "w", encoding="utf-8") as f:
        f.write(f"# Run log\n\n{request}\n")


def log_round(
    workspace: str, round_num: int, commit_id: str, passed: bool, score: int, sig: str, output: str
) -> None:
    """Append what happened this round: result, what changed since last round, the failure."""
    fence = "`" * 3
    status = "PASS" if passed else "FAIL"
    lines = [
        "",
        f"## Round {round_num} ({commit_id}): {status}",
        f"- tests passed: {score}",
        f"- failure: {'none' if passed else sig}",
        "- changed this round:",
        fence,
        changed_summary(workspace),
        fence,
    ]
    if not passed:
        lines += ["- output (first 1500 chars):", fence, output.strip()[:1500], fence]
    with open(os.path.join(loop_dir(workspace), "log.md"), "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _stopped() -> Event:
    return Event(kind="loop_done", text="stopped")


def _denied(answer: str) -> Event:
    return _stopped() if answer == "stop" else Event(kind="loop_done", text="stopped: approval declined")


# ---- the engine -------------------------------------------------------------


class Engine(Steerable):
    def __init__(
        self,
        channel: ControlChannel,
        ask_approval: Callable[[], bool] = lambda: False,
        model: Callable[[str, str], str] = call_role,
        check_ruff: Callable[[str], dict] = run_ruff,
        check_pytest: Callable[[str, str | None], dict] = run_pytest,
        prepare_env: Callable[[str], str] = ensure_env,
        max_rounds: int = MAX_ROUNDS,
        workspace: str | None = None,
        base_dir: str | None = None,
        intake: bool = False,
        ask_when_stuck: bool = False,
    ) -> None:
        super().__init__(channel, ask_approval)
        self.model = model
        self.check_ruff = check_ruff
        self.check_pytest = check_pytest
        self.prepare_env = prepare_env
        self.max_rounds = max_rounds
        self.workspace = workspace  # None: decided during intake or from the request
        self.base_dir = base_dir
        self.intake = intake
        self.ask_when_stuck = ask_when_stuck
        self.python: str | None = None

    def _call(self, role: str, text: str) -> str:
        """Call the model. All steering so far goes into the prompt, in order."""
        if self.steer_notes:
            notes = "\n".join(f"- {n}" for n in self.steer_notes)
            text += f"\n\n## Steering from the user (apply these, newest last)\n{notes}\n"
        return self.model(role, text)

    def run(self, request: str) -> Iterator[Event]:
        """Intake, plan, split, build, then check and fix. A crash leaves .loop/error.txt."""
        try:
            yield from self._run(request)
        except Exception:
            self._save_error()
            raise

    def _save_error(self) -> None:
        if self.workspace and os.path.isdir(self.workspace):
            with open(os.path.join(loop_dir(self.workspace), "error.txt"), "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())

    # -- intake --

    def _intake(self, request: str, ask_location: bool) -> Iterator[Event]:
        """Ask clarifying questions. Returns (brief, location_text), or None if the run ended."""
        yield Event(kind="phase_started", text="intake: a few questions first (/skip to start right away)")
        answers: list[tuple[str, str]] = []
        skipped = False
        for question in parse_questions(self.model("intake", request)):
            answer = yield from self._ask(question)
            if answer is STOP:
                yield _stopped()
                return None
            if answer is SKIP:
                skipped = True
                break
            answers.append((question, answer))
        location = ""
        if ask_location and not skipped:
            answer = yield from self._ask(
                "Where should the project be created? A local path or a GitHub URL (Enter: a new folder here)"
            )
            if answer is STOP:
                yield _stopped()
                return None
            if answer is SKIP:
                skipped = True
            else:
                location = answer
        brief = format_brief(request, answers)
        if not skipped:
            yield Event(kind="brief_ready", text=brief)
            answer = yield from self._ask("Start building with this brief? (y/n)")
            if answer is STOP:
                yield _stopped()
                return None
            if answer is not SKIP and answer.strip().lower() in ("n", "no"):
                yield Event(kind="loop_done", text="stopped: brief rejected, start again with a new prompt")
                return None
        return brief, location

    # -- sub-agents --

    def _files_from_model(self, role: str, prompt: str, only_file: str | None) -> dict[str, str]:
        """Ask a builder or fixer for files. One malformed reply gets one corrective retry."""
        reply = self._call(role, prompt)
        try:
            return files_from_reply(reply, only_file)
        except ValueError:
            self._save_bad_reply(role, reply)
        retry = (
            prompt
            + "\n\n## Format error\nYour previous reply had no files in the required format. "
            "Reply again with ONLY fenced blocks, each tagged with the language and the file "
            "path, for example: ```python path=app.py"
        )
        return files_from_reply(self._call(role, retry), only_file)

    def _save_bad_reply(self, role: str, reply: str) -> None:
        """Keep the raw reply in .loop/ so a format problem can be diagnosed afterwards."""
        if self.workspace:
            name = f"bad_reply_{role}_{abs(hash(reply)) % 10000}.txt"
            with open(os.path.join(loop_dir(self.workspace), name), "w", encoding="utf-8") as f:
                f.write(reply)

    def _run_builder(self, task: dict, context: dict[str, str], tests_text: str) -> dict[str, str]:
        only_file = task["files"][0] if len(task["files"]) == 1 else None
        return self._files_from_model("builder", build_task_prompt(task, context, tests_text), only_file)

    def _fix_tasks(
        self,
        affected: list[dict],
        files: dict[str, str],
        spec: str,
        tests_text: str,
        failure_output: str,
        protected: set[str],
    ) -> Iterator[Event]:
        """Fix the affected tasks in parallel. Returns {path: new text}."""
        for task in affected:
            yield Event(kind="agent_started", text=f"fixing {task['title']}")

        def work(task: dict) -> dict[str, str]:
            current = {p: files[p] for p in task["files"] if p in files}
            prompt = build_fixer_prompt(spec, tests_text, task, current, failure_output)
            only_file = task["files"][0] if len(task["files"]) == 1 else None
            return self._files_from_model("fixer", prompt, only_file)

        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(affected))) as pool:
            results = list(pool.map(work, affected))
        updates: dict[str, str] = {}
        for task, new in zip(affected, results, strict=True):
            for path, text in new.items():
                if path not in task["files"]:
                    # Protected tests, and also config files such as ruff.toml that would
                    # silence a check instead of fixing the code.
                    reason = "protected" if path in protected else "outside its task"
                    yield Event(kind="conflict", text=f"fixer for {task['id']} wrote {path} ({reason}); ignored")
                else:
                    updates[path] = text
            yield Event(kind="agent_done", text=f"fixed {task['title']}: {', '.join(sorted(new))}")
        return updates

    # -- the run --

    def _run(self, request: str) -> Iterator[Event]:
        if (yield from self._checkpoint()):
            yield _stopped()
            return

        # 1. intake and where the project goes
        brief = request
        location_text = ""
        if self.intake:
            result = yield from self._intake(request, ask_location=self.workspace is None)
            if result is None:
                return
            brief, location_text = result
        remote = None
        if self.workspace is None:
            try:
                location = resolve_location(location_text, self.base_dir or os.getcwd(), slugify(request))
            except ValueError as exc:
                yield Event(kind="error", text=str(exc))
                yield Event(kind="loop_done", text="failed: bad project location")
                return
            self.workspace, remote = location.path, location.remote
        ws = self.workspace
        init_repo(ws)
        if remote:
            add_remote(ws, remote)
        loop_dir(ws)
        yield Event(kind="repo_ready", text=ws)
        if remote:
            yield Event(kind="remote_set", text=remote)
        reset_log(ws, brief)

        # 2. spec and tests
        yield Event(kind="phase_started", text="planning: writing the spec and tests")
        plan = self._call("planner", brief)
        spec = extract_spec(plan)
        planner_files = parse_file_blocks(plan)
        if not planner_files:  # a plain ```python block is the v1-style single test file
            planner_files = {"test_solution.py": first_block(plan, "python") + "\n"}
        protected = set(planner_files)  # the tests and requirements.txt are the approved contract
        has_requirements = "requirements.txt" in planner_files
        tests_text = format_files({p: t for p, t in planner_files.items() if p != "requirements.txt"})
        with open(os.path.join(loop_dir(ws), "spec.md"), "w", encoding="utf-8") as f:
            f.write(spec + "\n")
        yield Event(kind="spec_written", text="spec and tests ready (.loop/spec.md)")

        # 3. split into tasks
        if (yield from self._checkpoint()):
            yield _stopped()
            return
        yield Event(kind="phase_started", text="splitting the spec into tasks")
        tasks, run_info = parse_split(self._call("splitter", f"## Spec\n{spec}\n\n## Tests\n{tests_text}"))
        if is_default_split(tasks):  # the split was unreadable: take the files from the spec instead
            tasks[0]["files"] = guess_files(spec) or tasks[0]["files"]
        if run_info:
            with open(os.path.join(loop_dir(ws), "run.json"), "w", encoding="utf-8") as f:
                json.dump(run_info, f, indent=2)
        yield Event(kind="tasks_created", text=f"{len(tasks)} task(s): " + ", ".join(t["title"] for t in tasks))

        # 4. parallel sub-agents, then collect their files
        yield Event(kind="phase_started", text="building")
        graph = build_graph(lambda task, context: self._run_builder(task, context, tests_text))
        outputs: dict[str, dict[str, str]] = {}
        stream = graph.stream(
            {"tasks": tasks, "outputs": {}},
            config={"max_concurrency": MAX_PARALLEL},
            stream_mode=["custom", "values"],
        )
        try:
            for mode, chunk in stream:
                if mode == "custom":
                    yield Event(**chunk)
                else:
                    outputs = chunk["outputs"]
                if (yield from self._checkpoint()):
                    yield _stopped()
                    return
        finally:
            stream.close()
        files, conflicts = collect_files(tasks, outputs, protected)
        for conflict in conflicts:
            yield Event(kind="conflict", text=conflict)
        yield Event(kind="files_collected", text=f"{len(files)} file(s) from {len(tasks)} task(s)")

        # 5. check and fix
        sigs: list[str] = []
        last_commit = ""
        outcome = "failed"
        round_num = 0
        limit = self.max_rounds
        stuck_prompts = 0
        while round_num < limit:
            round_num += 1
            if (yield from self._checkpoint()):
                yield _stopped()
                return
            yield Event(kind="phase_started", text=f"round {round_num}: write and check")

            answer = yield from self._gate(f"file_written: {len(files) + len(protected)} file(s)")
            if answer != "yes":
                yield _denied(answer)
                return
            write_files(ws, planner_files)  # always the approved copy
            write_files(ws, files)
            for path in [*planner_files, *files]:
                yield Event(kind="file_written", text=path)

            if has_requirements and self.python is None:
                answer = yield from self._gate("command_run: install requirements.txt")
                if answer != "yes":
                    yield _denied(answer)
                    return
                yield Event(kind="command_run", text="install requirements.txt (uv)")
                try:
                    self.python = self.prepare_env(ws)
                except RuntimeError as exc:
                    yield Event(kind="error", text=str(exc))
                    yield Event(kind="loop_done", text="failed: could not install requirements")
                    return

            answer = yield from self._gate("command_run: ruff check")
            if answer != "yes":
                yield _denied(answer)
                return
            yield Event(kind="command_run", text="ruff check")
            ruff_result = self.check_ruff(ws)
            if ruff_result.get("passed") is False:
                passed, score = False, 0
                failure_output = ruff_result["output"]
                sig = "TIMEOUT" if ruff_result.get("timed_out") else ruff_signature(failure_output)
                yield Event(kind="test_result", text=f"FAIL (ruff): {sig}")
            else:
                answer = yield from self._gate("command_run: pytest")
                if answer != "yes":
                    yield _denied(answer)
                    return
                yield Event(kind="command_run", text="pytest")
                pytest_result = self.check_pytest(ws, self.python)
                passed = pytest_result["passed"]
                failure_output = pytest_result["output"]
                score = count_passed(failure_output)
                sig = "PASS" if passed else failure_signature(failure_output, pytest_result["timed_out"])
                yield Event(kind="test_result", text=f"PASS: {score} passed" if passed else f"FAIL (pytest): {sig}")

            last_commit = commit_round(round_num, score, sig, workspace=ws)
            log_round(ws, round_num, last_commit, passed, score, sig, failure_output)

            if passed:
                yield Event(kind="loop_done", text=f"success: tests pass after {round_num} round(s)")
                return

            sigs.append(sig)
            stuck = len(sigs) >= 3 and len(set(sigs[-3:])) == 1
            if stuck or round_num == limit:
                reason = (
                    f"stuck: same failure '{sig}' 3 times in a row"
                    if stuck
                    else f"out of rounds ({limit}), last failure {sig}"
                )
                if self.ask_when_stuck and stuck_prompts < MAX_STUCK_PROMPTS:
                    stuck_prompts += 1
                    yield Event(kind="stuck", text=reason)
                    answer = yield from self._ask("Type 'retry', give guidance for the fixer, or 'stop'")
                    if answer is STOP or answer is SKIP or answer.strip().lower() in ("", "stop"):
                        outcome = f"failed: {reason}"
                        break
                    if answer.strip().lower() != "retry":
                        self.steer_notes.append(answer.strip())
                    limit += STUCK_RETRY_ROUNDS
                    sigs.clear()
                else:
                    outcome = f"failed: {reason}"
                    break

            if (yield from self._checkpoint()):
                yield _stopped()
                return
            affected = affected_tasks(tasks, failure_output)
            yield Event(
                kind="phase_started",
                text=f"round {round_num} failed ({sig}): fixing {len(affected)} task(s)",
            )
            updates = yield from self._fix_tasks(affected, files, spec, tests_text, failure_output, protected)
            files.update(updates)

        best = best_round(ws)
        if best and best["id"] != last_commit:
            rewind_to(best["id"], ws)
            yield Event(
                kind="rewound",
                text=f"project restored to round {best['round']} ({best['id']}), score {best['score']}",
            )
        yield Event(kind="loop_done", text=outcome)
