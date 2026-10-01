from ouroboros.contracts.chat_id_policy import (
    A2A_CHAT_ID_CEILING,
    SKILL_BRIDGE_CHAT_ID_MAX,
    SKILL_BRIDGE_CHAT_ID_MIN,
    WEB_UI_CHAT_ID,
    is_derived_a2a_chat_id,
    is_internal_chat_id,
)
from ouroboros.ingress.a2a_executor import derive_a2a_chat_id


def test_every_negative_chat_id_is_internal() -> None:
    assert is_internal_chat_id(SKILL_BRIDGE_CHAT_ID_MIN)
    assert is_internal_chat_id(SKILL_BRIDGE_CHAT_ID_MIN - 1)
    assert is_internal_chat_id(-1001)
    assert is_internal_chat_id(SKILL_BRIDGE_CHAT_ID_MAX)


def test_human_and_telegram_chat_ids_are_not_internal() -> None:
    assert not is_internal_chat_id(WEB_UI_CHAT_ID)
    assert not is_internal_chat_id(123456789)
    assert not is_derived_a2a_chat_id(WEB_UI_CHAT_ID)


def test_invalid_chat_ids_are_not_internal() -> None:
    assert not is_internal_chat_id(None)
    assert not is_internal_chat_id("")
    assert not is_internal_chat_id("not-an-int")
    assert not is_derived_a2a_chat_id(None)


def test_the_two_allocator_windows_do_not_overlap() -> None:
    """The invariant the split rests on, stated once rather than implied by three constants."""
    assert A2A_CHAT_ID_CEILING < SKILL_BRIDGE_CHAT_ID_MIN <= SKILL_BRIDGE_CHAT_ID_MAX < 0
    for chat_id in range(SKILL_BRIDGE_CHAT_ID_MIN, SKILL_BRIDGE_CHAT_ID_MAX + 1):
        assert not is_derived_a2a_chat_id(chat_id)


def test_derived_ids_stay_below_the_ceiling_and_inside_the_json_safe_range() -> None:
    for task_id in ("t-1", "task-abcdef", "0", "x" * 200):
        chat_id = derive_a2a_chat_id(task_id)
        assert chat_id <= A2A_CHAT_ID_CEILING
        assert is_derived_a2a_chat_id(chat_id)
        assert is_internal_chat_id(chat_id)
        # Survives a JSON round trip through a browser, where these ids are handed out.
        assert abs(chat_id) < 2**53


def test_derivation_is_stable_per_task_id() -> None:
    assert derive_a2a_chat_id("task-7") == derive_a2a_chat_id("task-7")
    assert derive_a2a_chat_id("task-7") != derive_a2a_chat_id("task-8")
