"""Factory functions for test doubles — work at module load time.

Unlike conftest fixtures, these are plain callables that can be used inside
``parametrize_from_dict`` dicts.  Each returns a ready-to-use mock or stub
matching the most common inline pattern found across the suite.

The 53+ inline ``llm = MagicMock(); llm.default_model.return_value = "test-model"``
boilerplate, the 31 ``safety.check_safety`` -> ``(True, "")`` patches, and the
4 duplicated ``SimpleNamespace(chat=SimpleNamespace(completions=...))`` shapes
are all replaced by one-liners here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from new_core.tools.interfaces import NullToolEvents


def make_mock_llm(
    response: Any = None,
    *,
    model: str = "test-model",
    usage: dict[str, int] | None = None,
) -> MagicMock:
    """A MagicMock LLM with ``default_model`` set; ``.chat`` pre-configured.

    ``response`` can be a dict (``{"content": ..., "tool_calls": ..., "finish_reason": ...}``),
    an ``LLMMessage``, or ``None`` (no canned response — the test sets ``.chat.return_value``).

    ``usage`` defaults to a minimal ``{"prompt_tokens": 10, "completion_tokens": 5}``.
    """
    llm = MagicMock()
    llm.default_model.return_value = model
    if response is not None:
        llm.chat.return_value = (response, usage or {"prompt_tokens": 10, "completion_tokens": 5})
    return llm


def make_loop_ctx(
    messages: list,
    *,
    task_id: str = "task-1",
    pending_events: list | None = None,
    pending_compaction: Any = None,
    tool_surface: Any = None,
) -> SimpleNamespace:
    """A minimal loop context for ``drive_loop`` tests.

    Matches the shape ``ToolContext`` exposes to the loop graph: ``events``,
    ``task_id``, ``messages``, ``pending_events``, ``_pending_compaction``,
    ``tool_surface``.
    """
    return SimpleNamespace(
        events=NullToolEvents(),
        task_id=task_id,
        messages=messages,
        pending_events=pending_events or [],
        _pending_compaction=pending_compaction,
        tool_surface=tool_surface,
    )


def make_mock_tool_ctx(
    tmp_path,
    *,
    scope: Any = None,
    task_id: str = "test-task",
    task_type: str = "task",
    repo_dir: Any = None,
    drive_root: Any = None,
) -> MagicMock:
    """A ``MagicMock(spec=ToolContext)`` with common attributes pre-set.

    The spec keeps attribute access honest — typos raise ``AttributeError``
    like a real ``ToolContext`` would.  Drive paths resolve to real dirs
    under ``tmp_path`` so observability code does not break.
    """
    from new_core.tools.context import ToolContext
    from tests.helpers.shared import TEST_SCOPE

    ctx = MagicMock(spec=ToolContext)
    ctx.events = NullToolEvents()
    ctx.task_id = task_id
    ctx.current_task_type = task_type
    ctx.scope = scope or TEST_SCOPE
    ctx.repo_dir = repo_dir if repo_dir is not None else tmp_path
    ctx.drive_root = drive_root if drive_root is not None else tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    ctx.drive_logs.return_value = logs
    ctx.emit_progress_fn = lambda *a, **kw: None
    return ctx


def permissive_safety(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
    """The ``(allowed=True, warning="")`` that 31 monkeypatches installed inline.

    Accepts any arguments the real ``check_safety`` would receive, so it can
    be used as a drop-in replacement via ``monkeypatch.setattr``.
    """
    return (True, "")


def make_openai_compat_fake(
    response_dict: dict | None = None,
    *,
    seen: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """An OpenAI SDK-shaped fake: ``client.chat.completions.create(**kwargs)``.

    ``response_dict`` defaults to a minimal ``choices``/``usage`` payload.
    ``seen`` (if provided) is updated with the kwargs passed to ``create``.
    """
    if seen is None:
        seen = {}
    if response_dict is None:
        response_dict = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    class _FakeResponse:
        def model_dump(self) -> dict[str, Any]:
            return response_dict  # type: ignore[return-value]

    def create(**kwargs: Any) -> _FakeResponse:
        seen.update(kwargs)
        return _FakeResponse()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def make_async_mock(
    return_value: Any = None,
    *,
    side_effect: Any = None,
) -> AsyncMock:
    """An ``AsyncMock`` from a result or an exception/side-effect sequence.

    Shorthand for the ``make_s3_loader``-style pattern: if ``side_effect`` is
    an Exception, it becomes a single raise; if it's a list, it's used as a
    call sequence; otherwise ``return_value`` is used.
    """
    if side_effect is not None:
        return AsyncMock(side_effect=side_effect)
    return AsyncMock(return_value=return_value)
