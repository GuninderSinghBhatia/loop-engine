import os
import subprocess
import sys


def _as_text(value) -> str:
    """On a timeout, subprocess may hand back None or bytes instead of a str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_pytest(workspace: str, timeout: int = 30):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", workspace, "-q"],
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


def run_ruff(workspace: str, timeout: int = 10):
    try:
        ruff = subprocess.run(
            [sys.executable, "-m", "ruff", "check", os.path.join(workspace, "solution.py"), "--no-respect-gitignore", "--select", "E9,F"],
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
