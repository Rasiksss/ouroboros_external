from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

import pytest
from pydantic import SecretStr

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.a2a.task_handles import A2aTaskHandles
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import TEST_CHAT_ENCRYPTION_KEY, TEST_CREDENTIALS_KEY

OTHER_KEY = base64.b64encode(b"j" * 64).decode("ascii")
TASK_ID = "task-7f3a"


def _scope(owner: str, agent: str, surface: SurfaceId, chat: str) -> OwnerScope:
    return OwnerScope(owner_id=owner, agent_id=agent, surface_id=surface, chat_id=chat)


def _handles(key: str) -> A2aTaskHandles:
    return A2aTaskHandles(encryption_key=SecretStr(key))


ALICE = _scope("alice", "main", SurfaceId.BROWSER, "1")


def test_a_handle_is_the_task_id_and_the_documented_mac() -> None:
    derived = hmac.new(base64.b64decode(TEST_CREDENTIALS_KEY), b"ouroboros:a2a-task-handle:v1", hashlib.sha256).digest()
    payload = json.dumps(["a2a-task", "crm", TASK_ID, "alice", "main"], separators=(",", ":")).encode("utf-8")
    mac = hmac.new(derived, payload, hashlib.sha256).hexdigest()[:32]

    handle = _handles(TEST_CREDENTIALS_KEY).seal(ALICE, "crm", TASK_ID)

    assert handle == f"{TASK_ID}~{mac}"
    assert re.fullmatch(rf"{TASK_ID}~[0-9a-f]{{32}}", handle)


def test_a_handle_opens_for_the_owner_agent_and_remote_agent_that_sealed_it() -> None:
    handles = _handles(TEST_CREDENTIALS_KEY)

    assert handles.open(ALICE, "crm", handles.seal(ALICE, "crm", TASK_ID)) == TASK_ID


def test_a_task_id_holding_the_separator_round_trips() -> None:
    handles = _handles(TEST_CREDENTIALS_KEY)

    assert handles.open(ALICE, "crm", handles.seal(ALICE, "crm", "a~b")) == "a~b"


@parametrize_from_dict(
    {
        "another owner": {"scope": _scope("bob", "main", SurfaceId.BROWSER, "1"), "remote_agent_id": "crm"},
        "another agent": {"scope": _scope("alice", "helper", SurfaceId.BROWSER, "1"), "remote_agent_id": "crm"},
        "another remote agent": {"scope": ALICE, "remote_agent_id": "hr"},
    }
)
def test_a_handle_does_not_open_for_another_owner_agent_or_remote_agent(scope, remote_agent_id) -> None:
    handles = _handles(TEST_CREDENTIALS_KEY)
    handle = handles.seal(ALICE, "crm", TASK_ID)

    assert handles.open(scope, remote_agent_id, handle) is None


def test_a_handle_opens_for_another_chat_and_surface_of_the_same_owner_and_agent() -> None:
    handles = _handles(TEST_CREDENTIALS_KEY)
    handle = handles.seal(_scope("alice", "main", SurfaceId.BROWSER, "c1"), "crm", TASK_ID)

    assert handles.open(_scope("alice", "main", SurfaceId.DIALOGS, "c2"), "crm", handle) == TASK_ID


def _forged_cases() -> dict[str, dict[str, str]]:
    handle = _handles(TEST_CREDENTIALS_KEY).seal(ALICE, "crm", TASK_ID)
    mac = handle.rpartition("~")[2]
    flipped = ("0" if mac[0] != "0" else "1") + mac[1:]
    return {
        "a bare task id": {"value": TASK_ID},
        "an empty value": {"value": ""},
        "a separator alone": {"value": "~"},
        "no task id": {"value": f"~{mac}"},
        "one character of the mac changed": {"value": f"{TASK_ID}~{flipped}"},
        "an uppercase mac": {"value": f"{TASK_ID}~{mac.upper()}"},
        "a truncated mac": {"value": f"{TASK_ID}~{mac[:-1]}"},
        "a full-length digest": {"value": f"{handle}{'0' * 32}"},
        "another task id under the mac": {"value": f"task-other~{mac}"},
        "a mac that is not ascii": {"value": f"{TASK_ID}~{'é' * 32}"},
        "surrounding whitespace": {"value": f" {handle} "},
    }


@parametrize_from_dict(_forged_cases())
def test_a_bare_or_forged_task_id_does_not_open(value) -> None:
    assert _handles(TEST_CREDENTIALS_KEY).open(ALICE, "crm", value) is None


def test_a_changed_key_does_not_open_a_handle() -> None:
    handle = _handles(TEST_CREDENTIALS_KEY).seal(ALICE, "crm", TASK_ID)

    assert _handles(OTHER_KEY).open(ALICE, "crm", handle) is None


def test_task_handles_built_elsewhere_over_the_same_key_open_the_handle() -> None:
    handle = _handles(TEST_CREDENTIALS_KEY).seal(ALICE, "crm", TASK_ID)

    assert A2aTaskHandles(encryption_key=SecretStr(TEST_CREDENTIALS_KEY)).open(ALICE, "crm", handle) == TASK_ID


@parametrize_from_dict(
    {
        "empty": {"key": ""},
        "not base64": {"key": "not-a-key-s3cr3t!"},
        "sixteen bytes": {"key": base64.b64encode(b"s3cr3t-sixteen!!").decode("ascii")},
        "a chat encryption key": {"key": TEST_CHAT_ENCRYPTION_KEY},
    }
)
def test_an_invalid_key_is_refused_naming_the_setting_without_the_value(key) -> None:
    with pytest.raises(ValueError) as caught:
        _handles(key)

    assert "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY" in str(caught.value)
    if key:
        assert key not in str(caught.value)
    assert "s3cr3t" not in str(caught.value)
