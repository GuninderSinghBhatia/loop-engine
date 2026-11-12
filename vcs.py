"""Git helpers for the workspace: one repo per run, one commit per round."""

import os
import shutil
import stat
import subprocess

WORKSPACE = "workspace"

# Commit identity is passed per command so we never touch the user's git config.
_IDENTITY = ["-c", "user.name=loop-engine", "-c", "user.email=loop-engine@localhost"]


def git(*args: str) -> str:
    """Run `git <args>` inside WORKSPACE. Return its output, raise if git fails."""
    result = subprocess.run(
        ["git", *args],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def _force_remove(func, path, _exc):
    """rmtree helper: git marks object files read-only, which blocks deletes on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def init_repo(fresh: bool = True) -> None:
    """Create the workspace repo. fresh=True deletes workspace/.git first (one repo per run)."""
    os.makedirs(WORKSPACE, exist_ok=True)
    dot_git = os.path.join(WORKSPACE, ".git")
    if fresh and os.path.isdir(dot_git):
        shutil.rmtree(dot_git, onexc=_force_remove)
    git("init", "-q")
    with open(os.path.join(WORKSPACE, ".gitignore"), "w", encoding="utf-8") as f:
        # log.md is ignored so it isn't snapshotted: a rewind must not erase later log entries.
        f.write("__pycache__/\n.pytest_cache/\n.ruff_cache/\nlog.md\n")


def commit_round(round_num: int, score: int, note: str = "") -> str:
    """Snapshot the workspace as one commit. Return the short commit id.

    score is how many tests passed; it goes in the message so best_round() can find it.
    --allow-empty means a round that changed nothing still gets a commit.
    """
    git("add", "-A")
    message = f"round {round_num} | score={score} | {note}".rstrip(" |")
    git(*_IDENTITY, "commit", "-q", "--allow-empty", "-m", message)
    return git("rev-parse", "--short", "HEAD").strip()


def changed_summary() -> str:
    """Files changed by the latest commit, as `git show --stat` prints them."""
    return git("show", "--stat", "--format=", "HEAD").strip()


def history() -> list[dict]:
    """Return every round commit, oldest first: {id, round, score, note}."""
    try:
        out = git("log", "--reverse", "--format=%h%x09%s")
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


def best_round() -> dict | None:
    """The round with the highest score; the earliest one wins ties."""
    rounds = history()
    if not rounds:
        return None
    return max(rounds, key=lambda r: (r["score"], -r["round"]))


def rewind_to(commit_id: str) -> None:
    """Restore the workspace files to how they were at commit_id.

    This checks the files out; it does not delete history, so the bad rounds stay in the log.
    """
    git("checkout", commit_id, "--", ".")
