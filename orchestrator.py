import os
import re

from dotenv import load_dotenv
from langchain_groq import ChatGroq

from config import MODELS
from runner import run_pytest, run_ruff

load_dotenv()

MAX_ROUNDS = 3
WORKSPACE = "workspace"


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
    errors = re.findall(r"^ERROR \S+ - (\w+)", output, re.M)
    return ",".join(sorted(set(tests + errors))) or "UNKNOWN"


def ruff_signature(output: str) -> str:
    """Stable label for ruff output: sorted rule codes, no line numbers."""
    new_style = re.findall(r"^([A-Z]+\d+|invalid-syntax)\b", output, re.M)
    old_style = re.findall(r":\d+:\d+: ([A-Z]+\d+)", output)
    codes = new_style + old_style
    return ",".join(sorted(set(codes))) or "UNKNOWN"


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
    with open(f"roles/{role}.md", "r", encoding="utf-8") as f:
        system_prompt = f.read()
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


def run(request: str) -> bool:
    """Plan, build, then check and fix for up to MAX_ROUNDS. True if the tests pass."""
    plan = call_role("planner", request)
    spec = first_block(plan, "markdown")
    tests = first_block(plan, "python")
    print("Planner done: spec and tests ready.")

    code = first_block(call_role("builder", build_builder_prompt(spec, tests)), "python")

    sigs = []
    for round_num in range(1, MAX_ROUNDS + 1):
        write_workspace("solution.py", code)
        write_workspace("test_solution.py", tests)  # always the approved copy

        ruff_result = run_ruff(WORKSPACE)
        if ruff_result["passed"] is False:
            failure_output = ruff_result["output"]
            sig = ruff_signature(failure_output)
        else:
            pytest_result = run_pytest(WORKSPACE)
            if pytest_result["passed"]:
                print(f"Success: tests pass after {round_num} round(s).")
                return True
            failure_output = pytest_result["output"]
            sig = failure_signature(failure_output, pytest_result["timed_out"])

        print(f"Round {round_num} failed: {sig}")
        sigs.append(sig)
        if len(sigs) >= 3 and len(set(sigs[-3:])) == 1:
            print(f"Stuck: same failure '{sig}' 3 times in a row. Stopping.")
            return False
        if round_num == MAX_ROUNDS:
            break

        fix_reply = call_role("fixer", build_fixer_prompt(spec, tests, code, failure_output))
        code = first_block(fix_reply, "python")

    print(f"Out of rounds ({MAX_ROUNDS}). Last failure: {sigs[-1]}")
    return False


if __name__ == "__main__":
    run(input("Enter your query : "))
