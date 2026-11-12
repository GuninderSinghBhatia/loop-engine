"""Where the new project goes: a local path or a GitHub repo address."""

import re
from dataclasses import dataclass
from pathlib import Path

_GITHUB = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)


@dataclass
class Location:
    path: str  # the local project folder
    remote: str | None = None  # a GitHub URL to use as `origin`, if one was given


def slugify(text: str) -> str:
    """A short folder name from the request: first few words, lowercase, dashes."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:4]) or "project"


def _is_empty_project_dir(path: Path) -> bool:
    return not any(child.name != ".git" for child in path.iterdir())


def _unique_dir(base: Path, name: str) -> Path:
    candidate = base / name
    n = 2
    while candidate.exists() and not (candidate.is_dir() and _is_empty_project_dir(candidate)):
        candidate = base / f"{name}-{n}"
        n += 1
    return candidate


def resolve_location(text: str, base: str, slug: str) -> Location:
    """Turn what the user typed into a project folder.

    blank        -> <base>/<slug> (a fresh folder, never an existing one)
    GitHub URL   -> <base>/<repo name>, with the URL remembered as `origin`
    local path   -> that folder; it must not exist yet, or be empty
    """
    text = text.strip()
    base_path = Path(base)
    if not text:
        return Location(str(_unique_dir(base_path, slug)))
    match = _GITHUB.match(text)
    if match:
        return Location(str(_unique_dir(base_path, match["repo"])), remote=text)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base_path / path
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"{path} is a file, not a folder")
        if not _is_empty_project_dir(path):
            raise ValueError(f"{path} is not empty; give an empty or new folder")
    return Location(str(path))
