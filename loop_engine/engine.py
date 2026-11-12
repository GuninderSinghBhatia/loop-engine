"""The real engine: plan, build, then check and fix. v1's loop, but it emits events.

It never prints. At safe points between steps it reads the control channel
(pause, stop, steer, approvals), so the CLI can steer a live run.
"""

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from dotenv import load_dotenv
from langchain_groq import ChatGroq

from loop_engine.config import MODELS
from loop_engine.control import ControlChannel
from loop_engine.events import Event
from loop_engine.runner import run_pytest, run_ruff
from loop_engine.steering import Steerable
from loop_engine.vcs import (
    WORKSPACE,
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
LOG_PATH = os.path.join(WORKSPACE, "log.md")


# ---- pure helpers (unchanged from v1) --------------------------------------


def extract_code(result: str, lang: str = "python") -> list[str]:
    """Return the contents of every ```<lang> block in the reply."""
    pattern = rf"```{re.escape(lang)}[ \t]*\n(.*?)```"
    return [block.strip() for block in re.findall(pattern, result, re.DOTALL)]


def first_block(reply: str, lang: str) -> str:
    """Return the first ```<lang> block, or raise if the model forgot it."""
    blocks = extract_code(reply, lang)
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


def build_builder_prompt(spec: str, tests: str) -> str:
    """The builder sees the approved spec and tests."""
    return f"## Spec\n{spec}\n\n## Tests\n{tests}\n"


def build_fixer_prompt(spec: str, tests: str, code: str, failure_output: str) -> str:
    """One prompt for the fixer: what it should do, what exists, and what went wrong."""
    return (
        f"## Spec\n{spec}\n\n"
        f"## Tests\n{tests}\n\n"
        f"## Current solution.py\n{code}\n\n"
        f"## Failure output\n{failure_output}\n"
    )


def call_role(role: str, user_text: str) -> str:
    """Call the model for one role: its prompt file is the system prompt."""
    system_prompt = (ROLES_DIR / f"{role}.md").read_text(encoding="utf-8")
    model = ChatGroq(model=MODELS[role], temperature=0)
    response = model.invoke([("system", system_prompt), ("human", user_text)])
    return response.content


def write_workspace(name: str, text: str) -> str:
    """Write one file into the workspace and return its path."""
    os.makedirs(WORKSPACE, exist_ok=True)
    path = os.path.join(WORKSPACE, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def reset_log(request: str) -> None:
    """Start a fresh log.md for this run."""
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(f"# Run log\n\nRequest: {request}\n")


def log_round(round_num: int, commit_id: str, passed: bool, score: int, sig: str, output: str) -> None:
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
        changed_summary(),
        fence,
    ]
    if not passed:
        lines += ["- output (first 1500 chars):", fence, output.strip()[:1500], fence]
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _stopped() -> Event:
    return Event(kind="loop_done", text="stopped")


def _declined() -> Event:
    return Event(kind="loop_done", text="stopped: approval declined")


# ---- the engine -------------------------------------------------------------


class Engine(Steerable):
    def __init__(
        self,
        channel: ControlChannel,
        ask_approval: Callable[[], bool] = lambda: False,
        model: Callable[[str, str], str] = call_role,
        check_ruff: Callable[[str], dict] = run_ruff,
        check_pytest: Callable[[str], dict] = run_pytest,
        max_rounds: int = MAX_ROUNDS,
    ) -> None:
        super().__init__(channel, ask_approval)
        self.model = model
        self.check_ruff = check_ruff
        self.check_pytest = check_pytest
        self.max_rounds = max_rounds

    def _call(self, role: str, text: str) -> str:
        """Call the model. All steering so far goes into the prompt, in order."""
        if self.steer_notes:
            notes = "\n".join(f"- {n}" for n in self.steer_notes)
            text += f"\n\n## Steering from the user (apply these, newest last)\n{notes}\n"
        return self.model(role, text)

    def run(self, request: str) -> Iterator[Event]:
        """Plan, build, then check and fix for up to max_rounds."""
        if (yield from self._checkpoint()):
            yield _stopped()
            return
        yield Event(kind="phase_started", text="planning: writing the spec and tests")
        plan = self._call("planner", request)
        spec = first_block(plan, "markdown")
        tests = first_block(plan, "python")
        yield Event(kind="spec_written", text="spec and tests ready")

        if (yield from self._checkpoint()):
            yield _stopped()
            return
        yield Event(kind="phase_started", text="building")
        code = first_block(self._call("builder", build_builder_prompt(spec, tests)), "python")

        init_repo()
        reset_log(request)
        sigs: list[str] = []
        last_commit = ""
        outcome = "failed"
        for round_num in range(1, self.max_rounds + 1):
            if (yield from self._checkpoint()):
                yield _stopped()
                return
            yield Event(kind="phase_started", text=f"round {round_num}: write and check")

            answer = yield from self._gate("file_written: solution.py, test_solution.py")
            if answer != "yes":
                yield _stopped() if answer == "stop" else _declined()
                return
            write_workspace("solution.py", code)
            write_workspace("test_solution.py", tests)  # always the approved copy
            yield Event(kind="file_written", text="solution.py")
            yield Event(kind="file_written", text="test_solution.py")

            answer = yield from self._gate("command_run: ruff check")
            if answer != "yes":
                yield _stopped() if answer == "stop" else _declined()
                return
            yield Event(kind="command_run", text="ruff check")
            ruff_result = self.check_ruff(WORKSPACE)
            if ruff_result.get("passed") is False:
                passed, score = False, 0
                failure_output = ruff_result["output"]
                sig = "TIMEOUT" if ruff_result.get("timed_out") else ruff_signature(failure_output)
                yield Event(kind="test_result", text=f"FAIL (ruff): {sig}")
            else:
                answer = yield from self._gate("command_run: pytest")
                if answer != "yes":
                    yield _stopped() if answer == "stop" else _declined()
                    return
                yield Event(kind="command_run", text="pytest")
                pytest_result = self.check_pytest(WORKSPACE)
                passed = pytest_result["passed"]
                failure_output = pytest_result["output"]
                score = count_passed(failure_output)
                sig = "PASS" if passed else failure_signature(failure_output, pytest_result["timed_out"])
                result_text = f"PASS: {score} passed" if passed else f"FAIL (pytest): {sig}"
                yield Event(kind="test_result", text=result_text)

            last_commit = commit_round(round_num, score, sig)
            log_round(round_num, last_commit, passed, score, sig, failure_output)

            if passed:
                yield Event(kind="loop_done", text=f"success: tests pass after {round_num} round(s)")
                return

            sigs.append(sig)
            if len(sigs) >= 3 and len(set(sigs[-3:])) == 1:
                outcome = f"failed: stuck, same failure '{sig}' 3 times in a row"
                break
            if round_num == self.max_rounds:
                outcome = f"failed: out of rounds ({self.max_rounds}), last failure {sigs[-1]}"
                break

            if (yield from self._checkpoint()):
                yield _stopped()
                return
            yield Event(kind="phase_started", text=f"round {round_num} failed ({sig}): fixing")
            fix_reply = self._call("fixer", build_fixer_prompt(spec, tests, code, failure_output))
            code = first_block(fix_reply, "python")

        best = best_round()
        if best and best["id"] != last_commit:
            rewind_to(best["id"])
            yield Event(
                kind="rewound",
                text=f"workspace restored to round {best['round']} ({best['id']}), score {best['score']}",
            )
        yield Event(kind="loop_done", text=outcome)
