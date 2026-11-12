"""Deterministic checks: ruff, then pytest, over a whole multi-file project."""

import subprocess
import sys


def _as_text(value) -> str:
    """On a timeout, subprocess may hand back None or bytes instead of a str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_pytest(workspace: str, python: str | None = None, timeout: int = 60):
    """Run the project's tests with `python` (the project's own venv if it has one)."""
    try:
        result = subprocess.run(
            [python or sys.executable, "-m", "pytest", ".", "-q", "-p", "no:cacheprovider"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return {
            "passed": result.returncode == 0,
            "output": result.stdout + "\n" + result.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        partial = _as_text(e.stdout) + "\n" + _as_text(e.stderr)
        return {
            "passed": False,
            "output": f"Timed out after {timeout}s.\nPartial output:\n{partial}",
            "timed_out": True,
        }


def run_ruff(workspace: str, timeout: int = 20):
    """Static checks (syntax errors and pyflakes rules) over every .py file in the project."""
    try:
        ruff = subprocess.run(
            [
                sys.executable, "-m", "ruff", "check", ".",
                # --isolated: ignore any ruff.toml / pyproject.toml the project contains, so a
                # fixer cannot silence the check. Tests are the planner's, not ours to lint.
                "--isolated", "--no-respect-gitignore", "--select", "E9,F",
                "--exclude", ".venv,.loop,.git,__pycache__,test_*.py,*_test.py,conftest.py",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return {
            "passed": ruff.returncode == 0,
            "output": ruff.stdout + "\n" + ruff.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        partial = _as_text(e.stdout) + "\n" + _as_text(e.stderr)
        return {
            "passed": False,
            "output": f"Timed out after {timeout}s.\nPartial output:\n{partial}",
            "timed_out": True,
        }
