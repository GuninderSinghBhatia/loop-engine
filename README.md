# Loop Engine v2

An interactive CLI that builds, tests and fixes a project from a prompt, while you watch and steer.

```
uv sync
# put GROQ_API_KEY=... in .env (see .env.example)
uv run loop            # or: uv run loop --fake   (scripted engine, no model calls)
```

Run it from the folder where you want new projects to appear.

## A session

1. Type a project idea at the `loop>` prompt.
2. The engine asks a few clarifying questions (one at a time, answer in the prompt), then asks where
   the project should go: a local path, a GitHub URL, or Enter for a new folder here. You confirm a short
   brief. `/skip` skips all of this and starts building from the prompt alone.
3. The build streams live: spec, task split, parallel sub-agents, collected files, ruff and pytest, fix rounds.
4. When the tests pass, a project with a UI is launched for you (web app in the browser, desktop app as a window).

While a run is active, typed text answers the engine's question or **steers** the build; every steering
message is added to the prompts of the later steps.

## Commands

| Command | What it does |
| --- | --- |
| `/pause`, `/resume`, `/stop` | Control the running loop (Ctrl-C also pauses) |
| `/skip` | Skip the questions and start now |
| `/approve on\|off` | Ask y/n before files are written or commands run (default off) |
| `/status` | Current state and project folder |
| `/run`, `/run stop` | Start or stop the built project; its output shows in the session |
| `/browser` | Open the running web app in the browser |
| `/open` | Open the project folder in your editor (`LOOP_EDITOR`, default `code`) |
| `/push` | Push the project to the GitHub repo you gave (only when you type it) |
| `/help`, `/exit` | |

Pause, stop and steering take effect at safe points between steps, not in the middle of a model call.

## What gets built

Each project lives in its own folder, as its own git repo with one commit per round. The engine's records
are in `<project>/.loop/`: `spec.md`, `log.md`, `run.json` (how to start it) and `error.txt` if a run crashed.
Dependencies go into the project's own `.venv` (installed with `uv`), never into the engine's.

If the same failure repeats three times, or the rounds run out, the engine asks you whether to `retry`,
give guidance for the fixer, or `stop`. When a run ends without passing, the project is restored to its
best-scoring round.

## Layout

```
loop_engine/
  cli.py        the session (prompt_toolkit + Rich), slash commands
  engine.py     the real engine: emits events, never prints
  fake_engine.py  scripted engine for developing the CLI
  steering.py   pause / stop / steer / approvals / questions, shared by both engines
  control.py    the control channel (urgent signals jump the queue)
  pipeline.py   task split, LangGraph fan-out of sub-agents, file collection
  intake.py  project.py  env.py  runner.py  vcs.py  launcher.py
  roles/        the system prompts: intake, planner, splitter, builder, fixer
tests/          engine tests use a fake model and fake runners; no network needed
```

Develop with `uv run pytest` and `uv run ruff check`.
