"""Git helpers for a project folder: one repo per run, one commit per round."""

import os
import shutil
import stat
import subprocess

WORKSPACE = "workspace"  # legacy default; the engine now passes the real folder

# Commit identity is passed per command so we never touch the user's git config.
_IDENTITY = ["-c", "user.name=loop-engine", "-c", "user.email=loop-engine@localhost"]

GITIGNORE = "__pycache__/\n.pytest_cache/\n.ruff_cache/\n.venv/\n.loop/\n"


def git(*args: str, workspace: str = WORKSPACE, timeout: int = 30) -> str:
    """Run `git <args>` inside the project folder. Return its output, raise if git fails."""
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def _force_remove(func, path, _exc):
    """rmtree helper: git marks object files read-only, which blocks deletes on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def init_repo(workspace: str = WORKSPACE, fresh: bool = False) -> None:
    """Create the project repo. fresh=True deletes an existing .git first (never the default)."""
    os.makedirs(workspace, exist_ok=True)
    dot_git = os.path.join(workspace, ".git")
    if fresh and os.path.isdir(dot_git):
        shutil.rmtree(dot_git, onexc=_force_remove)
    git("init", "-q", workspace=workspace)
    with open(os.path.join(workspace, ".gitignore"), "w", encoding="utf-8") as f:
        # .loop/ holds the run log: a rewind must not erase later log entries.
        f.write(GITIGNORE)


def add_remote(workspace: str, url: str) -> None:
    """Point `origin` at the repo the user asked for. Nothing is pushed here."""
    git("remote", "add", "origin", url, workspace=workspace)


def push(workspace: str) -> str:
    """Push the current branch to origin. Only ever called when the user asks (/push)."""
    return git("push", "-u", "origin", "HEAD", workspace=workspace, timeout=180)


def commit_round(round_num: int, score: int, note: str = "", workspace: str = WORKSPACE) -> str:
    """Snapshot the project as one commit. Return the short commit id.

    score is how many tests passed; it goes in the message so best_round() can find it.
    --allow-empty means a round that changed nothing still gets a commit.
    """
    git("add", "-A", workspace=workspace)
    message = f"round {round_num} | score={score} | {note}".rstrip(" |")
    git(*_IDENTITY, "commit", "-q", "--allow-empty", "-m", message, workspace=workspace)
    return git("rev-parse", "--short", "HEAD", workspace=workspace).strip()


def changed_summary(workspace: str = WORKSPACE) -> str:
    """Files changed by the latest commit, as `git show --stat` prints them."""
    return git("show", "--stat", "--format=", "HEAD", workspace=workspace).strip()


def history(workspace: str = WORKSPACE) -> list[dict]:
    """Return every round commit, oldest first: {id, round, score, note}."""
    try:
        out = git("log", "--reverse", "--format=%h%x09%s", workspace=workspace)
    except RuntimeError:  # a fresh repo with no commits yet
        return []
    rounds = []
    for line in out.splitlines():
        commit_id, _, subject = line.partition("\t")
        parts = [p.strip() for p in subject.split("|")]
        if len(parts) < 2 or not parts[0].startswith("round "):
            continue
        rounds.append(
            {
                "id": commit_id,
                "round": int(parts[0].split()[1]),
                "score": int(parts[1].removeprefix("score=")),
                "note": parts[2] if len(parts) > 2 else "",
            }
        )
    return rounds


def best_round(workspace: str = WORKSPACE) -> dict | None:
    """The round with the highest score; the earliest one wins ties."""
    rounds = history(workspace)
    if not rounds:
        return None
    return max(rounds, key=lambda r: (r["score"], -r["round"]))


def rewind_to(commit_id: str, workspace: str = WORKSPACE) -> None:
    """Restore the project files to how they were at commit_id.

    This checks the files out; it does not delete history, so the bad rounds stay in the log.
    """
    git("checkout", commit_id, "--", ".", workspace=workspace)
