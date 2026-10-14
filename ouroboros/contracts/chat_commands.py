"""The commands the runtime owns, and the ones it used to.

One vocabulary for the addressing gate, intent classification, the
attachment-with-command rejection, the browser operator-command gate and
`supervisor/commands.py` dispatch. It lives in contracts because two sides need
the same answer and neither may import the other: the supervisor classifies the
request and runs it, and a transport has to know a message is a command before it
decides whether an attachment may ride along.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from new_core.identity import SurfaceId
from ouroboros.contracts.tool_confirmation import CONFIRMATION_APPROVED, CONFIRMATION_DENIED

REASONING_COMMAND = "/reasoning"

CONFIRM_COMMAND = "/confirm"

TYPED_ANSWER_SURFACES = frozenset({SurfaceId.DIALOGS})

#: How much of a request id the question shows, and the shortest an answer may name.
CONFIRMATION_ID_HINT_LEN = 8

RUNTIME_SLASH_COMMANDS = frozenset({"/status", "/restart", "/stop", "/new", "/jobs", REASONING_COMMAND})

#: Answered by the runtime rather than queued: handed one as work, the agent reports it ran. A
#: command leaving `RUNTIME_SLASH_COMMANDS` for good belongs here, not nowhere.
RETIRED_SLASH_COMMANDS = frozenset({"/clean"})

_REASONING_ARGUMENTS = {"on": True, "off": False}

CONFIRMATION_APPROVALS = frozenset({"да", "yes", "подтверждаю", "ok", "ок"})

CONFIRMATION_REFUSALS = frozenset({"нет", "no", "отмена", "отменяю"})

#: From the shown head up to the whole id a client sends. Shorter would read a number the owner
#: wrote for the agent as a request nobody opened, and swallow the message.
_CONFIRMATION_ID_RE = re.compile(rf"^[0-9a-f]{{{CONFIRMATION_ID_HINT_LEN},32}}$")


@dataclass(frozen=True)
class ConfirmationAnswer:
    """Empty ``confirmation_id`` means the answer applies to whatever that scope has open."""

    decision: str
    confirmation_id: str = ""


def normalize_command(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return normalized.strip(" \t\r\n.!?,;:")


def reasoning_visibility(text: str) -> bool | None:
    """Whether `/reasoning` asks for the agent's narration on or off, or names no argument."""
    parts = normalize_command(text).split()
    if len(parts) != 2 or parts[0] != REASONING_COMMAND:
        return None
    return _REASONING_ARGUMENTS.get(parts[1])


def _decision_word(word: str) -> str:
    if word in CONFIRMATION_APPROVALS:
        return CONFIRMATION_APPROVED
    if word in CONFIRMATION_REFUSALS:
        return CONFIRMATION_DENIED
    return ""


def confirmation_answer(text: str) -> ConfirmationAnswer | None:
    """A bare yes/no, that word plus the request id, or ``/confirm <id> <yes|no>``, and nothing else."""
    words = normalize_command(text).split(" ")
    if words[0] == CONFIRM_COMMAND:
        if len(words) != 3 or not _CONFIRMATION_ID_RE.match(words[1]):
            return None
        decision = _decision_word(words[2])
        return ConfirmationAnswer(decision, words[1]) if decision else None
    decision = _decision_word(words[0])
    if not decision:
        return None
    if len(words) == 1:
        return ConfirmationAnswer(decision)
    if len(words) == 2 and _CONFIRMATION_ID_RE.match(words[1]):
        return ConfirmationAnswer(decision, words[1])
    return None


def is_confirm_command(text: str) -> bool:
    return normalize_command(text).split(" ")[0] == CONFIRM_COMMAND


def is_confirmation_id(value: str) -> bool:
    return _CONFIRMATION_ID_RE.fullmatch(value) is not None


def runtime_slash_command(text: str) -> str:
    """The owned slash command this text invokes, or an empty string."""
    return runtime_command_parts(text)[0]


def runtime_command_parts(text: str) -> tuple[str, str]:
    """Return the exact first token and the original-cased remainder."""
    stripped = str(text or "").lstrip()
    if not stripped:
        return "", ""
    parts = stripped.split(maxsplit=1)
    command = parts[0].lower()
    if command not in RUNTIME_SLASH_COMMANDS:
        for legacy in ("/status", "/restart"):
            if command.startswith(legacy):
                return legacy, stripped[len(legacy) :].lstrip()
        return "", ""
    return command, parts[1] if len(parts) == 2 else ""


def retired_slash_command(text: str) -> str:
    """The retired command this text invokes, or an empty string."""
    stripped = str(text or "").lstrip()
    if not stripped:
        return ""
    token = normalize_command(stripped.split(maxsplit=1)[0])
    return token if token in RETIRED_SLASH_COMMANDS else ""


def answered_slash_command(text: str) -> str:
    """The command the runtime answers itself, owned or retired.

    What every gate over a command actually asks: such a message never becomes a task, so
    an attachment bound to one has no run to reach and a group message carrying one is
    addressed to the bot.
    """
    return runtime_slash_command(text) or retired_slash_command(text)


def is_slash_command(text: str) -> bool:
    """Whether a user message is a command rather than something to work on."""
    return str(text or "").strip().startswith("/")


__all__ = [
    "CONFIRMATION_ID_HINT_LEN",
    "CONFIRM_COMMAND",
    "REASONING_COMMAND",
    "RETIRED_SLASH_COMMANDS",
    "RUNTIME_SLASH_COMMANDS",
    "TYPED_ANSWER_SURFACES",
    "ConfirmationAnswer",
    "answered_slash_command",
    "confirmation_answer",
    "is_confirm_command",
    "is_confirmation_id",
    "is_slash_command",
    "normalize_command",
    "reasoning_visibility",
    "retired_slash_command",
    "runtime_command_parts",
    "runtime_slash_command",
]
