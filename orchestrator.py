import os
from langchain_groq import ChatGroq
from dotenv import load_dotenv
from config import MODELS
import re

from runner import run_pytest, run_ruff
load_dotenv()

MAX_ROUNDS = 3

llm = ChatGroq(
    model=MODELS['builder'],
    temperature=0,
)

query = input("Enter your query : ")
response = llm.invoke([("system" , "You are a builder agent and you will buid for the user whatever he put the queireis in you"), ("human" , query)])
result = response.content


def extract_code(result: str) -> list[str]:
    """Return the contents of every ```python block in the reply."""
    pattern = r"```python[ \t]*\n(.*?)```"
    return [block.strip() for block in re.findall(pattern, result, re.DOTALL)]


main_code = extract_code(result)
if not main_code:
    raise ValueError("No python code block found")

os.makedirs("workspace", exist_ok=True)
path = os.path.join("workspace", "solution.py")
with open(path, "w", encoding="utf-8") as f:
    f.write(main_code[0])
print(f"Saved code to {path}")


def failure_signature(output: str, timed_out: bool = False) -> str:
    """Stable label for a pytest failure: failing test names, error types, or TIMEOUT."""
    if timed_out:
        return "TIMEOUT"
    tests = re.findall(r"FAILED \S+::(\S+)", output)
    errors = re.findall(r"^ERROR \S+ - (\w+)", output, re.M)
    return ",".join(sorted(set(tests + errors))) or "UNKNOWN"


def ruff_signature(output: str) -> str:
    """Stable label for ruff output: sorted rule codes, no line numbers."""
    codes = re.findall(r":\d+:\d+: ([A-Z]+\d+)", output)
    return ",".join(sorted(set(codes))) or "UNKNOWN"


def build_fixer_prompt(spec: str, tests: str, code: str, failure_output: str) -> str:
    """One prompt for the fixer: what it should do, what exists, and what went wrong."""
    return (
        f"## Spec\n{spec}\n\n"
        f"## Tests\n{tests}\n\n"
        f"## Current solution.py\n{code}\n\n"
        f"## Failure output\n{failure_output}\n"
    )

sigs = []
for round_num in range(1, MAX_ROUNDS + 1) :
    ruff_result = run_ruff("workspace")

    if ruff_result["passed"] is False:
        sig = ruff_signature(ruff_result["output"])
    else:
        pytest_result = run_pytest("workspace")
        if pytest_result["passed"]:
            print(f"Success code is generated and it took {round_num} of rounds")
            break
        else:
            sig = failure_signature(pytest_result["output"], pytest_result["timed_out"])

    sigs.append(sig)
    if len(sigs) >= 3 and len(set(sigs[-3:])) == 1:
        print(f"Stuck: same failure '{sig}' 3 times in a row. Stopping.")
        break
else:
    print(f"Out of rounds ({MAX_ROUNDS}). Last failure: {sigs[-1]}")
