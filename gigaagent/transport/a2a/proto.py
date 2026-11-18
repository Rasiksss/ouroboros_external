"""A2A type helpers with SDK-layout compatibility."""
# mypy: ignore-errors

from __future__ import annotations

import uuid
from typing import Any

try:
    from a2a.helpers.proto_helpers import (  # type: ignore[import-not-found]
        get_message_text as _sdk_get_message_text,
        new_message as _sdk_new_message,
        new_text_artifact_update_event as _sdk_new_text_artifact_update_event,
        new_text_part as _sdk_new_text_part,
        new_text_status_update_event as _sdk_new_text_status_update_event,
    )
    from a2a.types.a2a_pb2 import (  # type: ignore[import-not-found]
        AgentCard,
        AgentSkill,
        Message,
        Role,
        Task,
        TaskArtifactUpdateEvent,
        TaskState,
        TaskStatus,
        TaskStatusUpdateEvent,
    )
    from google.protobuf.json_format import MessageToDict, ParseDict  # type: ignore[import-untyped]

    _PROTOBUF_SDK = True
except ImportError:
    from a2a.types import (  # type: ignore[import-not-found]
        AgentCapabilities,
        AgentCard,
        AgentSkill,
        Artifact,
        Message,
        Part,
        Role,
        Task,
        TaskArtifactUpdateEvent,
        TaskState,
        TaskStatus,
        TaskStatusUpdateEvent,
        TextPart,
    )

    _PROTOBUF_SDK = False

if _PROTOBUF_SDK:
    _TERMINAL_TASK_STATES = frozenset(
        {
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_REJECTED,
            "completed",
            "failed",
            "canceled",
            "rejected",
        }
    )
else:
    _TERMINAL_TASK_STATES = frozenset(
        {
            TaskState.completed,
            TaskState.failed,
            TaskState.canceled,
            TaskState.rejected,
            "completed",
            "failed",
            "canceled",
            "rejected",
        }
    )


def task_state_name(state: Any) -> str:
    """Normalize TaskState to a short lowercase name (e.g. ``completed``)."""
    if hasattr(state, "value"):
        return str(state.value).lower()
    if isinstance(state, str):
        return state.removeprefix("TASK_STATE_").lower()
    try:
        return TaskState.Name(int(state)).removeprefix("TASK_STATE_").lower()
    except (TypeError, ValueError):
        text = str(state).lower()
        if text.startswith("taskstate."):
            text = text.split(".", 1)[1]
        return text


def is_terminal_task_state(state: Any) -> bool:
    if state in _TERMINAL_TASK_STATES:
        return True
    return task_state_name(state) in {"completed", "failed", "canceled", "rejected"}


def task_to_dict(task: Task) -> dict[str, Any]:
    if hasattr(task, "model_dump"):
        return task.model_dump(by_alias=True, mode="json", exclude_none=True)
    return MessageToDict(task, preserving_proto_field_name=False)


def task_from_dict(data: dict[str, Any]) -> Task:
    if hasattr(Task, "model_validate"):
        return Task.model_validate(data)
    return ParseDict(data, Task())


def new_agent_skill(skill_id: str, name: str, description: str, tags: list[str]) -> AgentSkill:
    if not _PROTOBUF_SDK:
        return AgentSkill(
            id=skill_id,
            name=name,
            description=description,
            tags=tags,
        )
    return ParseDict(
        {
            "id": skill_id,
            "name": name,
            "description": description,
            "tags": tags,
        },
        AgentSkill(),
    )


def build_agent_card_proto(
    *,
    name: str,
    description: str,
    version: str,
    service_url: str,
    skills: list[AgentSkill],
    streaming: bool = True,
) -> AgentCard:
    if not _PROTOBUF_SDK:
        return AgentCard(
            name=name,
            description=description,
            version=version,
            url=service_url.rstrip("/") + "/",
            preferred_transport="JSONRPC",
            capabilities=AgentCapabilities(streaming=streaming),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            skills=skills,
        )
    return ParseDict(
        {
            "name": name,
            "description": description,
            "version": version,
            "supportedInterfaces": [
                {
                    "url": service_url.rstrip("/") + "/",
                    "protocolBinding": "JSONRPC",
                }
            ],
            "skills": [MessageToDict(skill, preserving_proto_field_name=False) for skill in skills],
            "capabilities": {"streaming": streaming},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
        },
        AgentCard(),
    )


def task_state_value(name: str) -> Any:
    normalized = str(name or "").strip().lower().replace("_", "-")
    if _PROTOBUF_SDK:
        attr = f"TASK_STATE_{normalized.upper().replace('-', '_')}"
        return getattr(TaskState, attr)
    attr = normalized.replace("-", "_")
    return getattr(TaskState, attr)


def _text_part(text: str) -> Any:
    if _PROTOBUF_SDK:
        return _sdk_new_text_part(text)
    return Part(root=TextPart(text=text))


def new_user_text_message(text: str, task_id: str | None = None) -> Any:
    """Build a user Message with a single text part.

    ``task_id`` names an existing remote task, which is how the outbound client answers a
    question a remote agent asked instead of starting a second task beside the first.
    """
    if _PROTOBUF_SDK:
        return _sdk_new_message(parts=[_text_part(text)], role=Role.ROLE_USER, task_id=task_id or None)
    return Message(
        role=Role.user,
        message_id=f"m-{uuid.uuid4().hex}",
        parts=[_text_part(text)],
        task_id=task_id or None,
    )


def get_message_text(message: Message) -> str:
    if _PROTOBUF_SDK:
        return _sdk_get_message_text(message)
    parts = getattr(message, "parts", []) or []
    texts: list[str] = []
    for part in parts:
        root = getattr(part, "root", part)
        text = getattr(root, "text", "")
        if text:
            texts.append(str(text))
    return "\n".join(texts)


def new_text_artifact_update_event(
    task_id: str,
    context_id: str,
    *,
    name: str,
    text: str,
    last_chunk: bool = True,
) -> TaskArtifactUpdateEvent:
    if _PROTOBUF_SDK:
        return _sdk_new_text_artifact_update_event(
            task_id,
            context_id,
            name=name,
            text=text,
            last_chunk=last_chunk,
        )
    return TaskArtifactUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        artifact=Artifact(
            artifact_id=name or f"artifact-{uuid.uuid4().hex}",
            name=name,
            parts=[_text_part(text)],
        ),
        last_chunk=last_chunk,
    )


def new_text_status_update_event(
    task_id: str,
    context_id: str,
    state: Any,
    text: str = "",
    *,
    final: bool | None = None,
) -> TaskStatusUpdateEvent:
    if final is None:
        final = is_terminal_task_state(state)
    if _PROTOBUF_SDK:
        return _sdk_new_text_status_update_event(task_id, context_id, state, text)
    message = None
    if text:
        message = Message(
            role=Role.agent,
            message_id=f"m-{uuid.uuid4().hex}",
            parts=[_text_part(text)],
        )
    return TaskStatusUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        status=TaskStatus(state=state, message=message),
        final=final,
    )


def new_task_event(
    task_id: str,
    context_id: str,
    state: TaskState | str = "submitted",
) -> Task:
    """Create a Task event for the A2A event queue (required before status updates)."""
    return new_task_for_test(task_id, state, context_id=context_id)


def new_task_for_test(task_id: str, state: TaskState | str, context_id: str = "ctx-1") -> Task:
    if isinstance(state, str):
        state = task_state_value(state)
    if not _PROTOBUF_SDK:
        return Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=state),
        )
    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=state),
    )
