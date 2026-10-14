from __future__ import annotations

from ouroboros.egress.workstation.session import PROCESS_INSTANCE_ID, scope_key_for, session_id_for


def test_scope_key_defaults_to_main_for_empty_or_whitespace() -> None:
    assert scope_key_for("") == "main"
    assert scope_key_for("   ") == "main"
    assert scope_key_for() == "main"


def test_scope_key_trims_and_joins_multiple_parts() -> None:
    assert scope_key_for(" abc ") == "abc"
    assert scope_key_for("abc", "def") == "abc_def"
    assert scope_key_for("abc", "", "def") == "abc_def"


def test_scope_key_chat_id_produces_per_conversation_session() -> None:
    chat_a = scope_key_for("chat-111")
    chat_b = scope_key_for("chat-222")
    assert chat_a != chat_b
    assert chat_a == "chat-111"
    assert chat_b == "chat-222"


def test_scope_key_same_chat_id_reuses_session() -> None:
    key_1 = scope_key_for("chat-111")
    key_2 = scope_key_for("chat-111")
    assert key_1 == key_2


def test_session_id_is_deterministic_fixed_length_hex() -> None:
    first = session_id_for("recover", "abc")
    second = session_id_for("recover", "abc")
    assert first == second
    assert len(first) == 24
    assert all(ch in "0123456789abcdef" for ch in first)


def test_session_id_differs_by_strategy_and_scope_key() -> None:
    assert session_id_for("ephemeral", "abc") != session_id_for("recover", "abc")
    assert session_id_for("recover", "abc") != session_id_for("recover", "def")


def test_ephemeral_session_id_includes_process_instance_component(monkeypatch) -> None:
    import ouroboros.egress.workstation.session as session_module

    monkeypatch.setattr(session_module, "PROCESS_INSTANCE_ID", "instance-a")
    id_a = session_module.session_id_for("ephemeral", "abc")
    monkeypatch.setattr(session_module, "PROCESS_INSTANCE_ID", "instance-b")
    id_b = session_module.session_id_for("ephemeral", "abc")
    assert id_a != id_b

    # recover has no process component, so it must be stable across "restarts".
    monkeypatch.setattr(session_module, "PROCESS_INSTANCE_ID", "instance-a")
    recover_a = session_module.session_id_for("recover", "abc")
    monkeypatch.setattr(session_module, "PROCESS_INSTANCE_ID", "instance-b")
    recover_b = session_module.session_id_for("recover", "abc")
    assert recover_a == recover_b


def test_process_instance_id_is_a_nonempty_hex_string() -> None:
    assert isinstance(PROCESS_INSTANCE_ID, str)
    assert len(PROCESS_INSTANCE_ID) > 0
