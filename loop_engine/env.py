"""The project's own Python environment, so its dependencies never touch ours."""

import subprocess
import sys
from pathlib import Path


def venv_python(workspace: str) -> Path:
    """Where the project's venv python lives (it may not exist yet)."""
    root = Path(workspace) / ".venv"
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def project_python(workspace: str) -> str:
    """The python to run the project with: its venv if it has one, otherwise ours."""
    py = venv_python(workspace)
    return str(py) if py.exists() else sys.executable


def ensure_env(workspace: str, timeout: int = 600) -> str:
    """Install requirements.txt into <project>/.venv and return that python.

    No requirements.txt means a standard-library project: use our own python.
    Raises RuntimeError with the installer's output if installation fails.
    """
    requirements = Path(workspace) / "requirements.txt"
    if not requirements.exists():
        return sys.executable
    py = venv_python(workspace)
    if not py.exists():
        _run(["uv", "venv", str(Path(workspace) / ".venv")], workspace, timeout)
    _run(
        ["uv", "pip", "install", "--python", str(py), "pytest", "-r", str(requirements)],
        workspace,
        timeout,
    )
    return str(py)


def _run(cmd: list[str], cwd: str, timeout: int) -> None:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{' '.join(cmd)} failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stdout}\n{result.stderr}".strip())
