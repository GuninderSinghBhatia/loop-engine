"""Intake: the engine asks clarifying questions before building (Phase 4)."""

import re

MAX_QUESTIONS = 5

_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(\S.*?)\s*$")


def parse_questions(reply: str) -> list[str]:
    """Turn the intake model's reply into a clean list of questions.

    The model is told to answer with a numbered list ("1. ...") or the single
    word NONE, but models add chatter, so only numbered lines are kept.
    """
    if reply.strip().upper() == "NONE":
        return []
    questions = []
    for line in reply.splitlines():
        match = _NUMBERED.match(line)
        if match:
            questions.append(match.group(1))
    return questions[:MAX_QUESTIONS]


def format_brief(request: str, answers: list[tuple[str, str]]) -> str:
    """The short brief the planner builds from: the request plus what the user clarified."""
    lines = ["# Project brief", "", "## Request", request.strip()]
    answered = [(q, a) for q, a in answers if a.strip()]
    if answered:
        lines += ["", "## Clarifications"]
        for question, answer in answered:
            lines.append(f"- {question} -> {answer.strip()}")
    return "\n".join(lines)
