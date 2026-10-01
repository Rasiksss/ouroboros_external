"""Tests for the compactor adapter's production microcompaction."""

_CLEARED = "[Tool result cleared to save context space]"


def _production_microcompact(messages, *, keep_recent_rounds=3, token_threshold=0, drive_root=None, task_id=""):
    """Run the production microcompaction as the compactor adapter owns it."""
    from ouroboros.agent.loop_adapters import TranscriptCompactorAdapter
    from ouroboros.agent.task_ledger import TaskLedger

    TranscriptCompactorAdapter(drive_root=drive_root, task_id=task_id, task_ledger=TaskLedger()).microcompact(
        messages, keep_recent_rounds=keep_recent_rounds, token_threshold=token_threshold
    )


def _make_round(idx: int, tool_name: str = "gigasearch", content: str = "", n_parallel: int = 1):
    """Create one assistant+tool(s) round."""
    calls = [{"id": f"c{idx}_{j}", "function": {"name": tool_name, "arguments": "{}"}} for j in range(n_parallel)]
    msgs = [{"role": "assistant", "content": f"reasoning {idx}", "tool_calls": calls}]
    for j in range(n_parallel):
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"c{idx}_{j}",
                "content": content or f"result_{idx}_{j} " + "x" * 60000,
            }
        )
    return msgs


def _build_session(rounds: int, tool_name: str = "gigasearch", n_parallel: int = 1):
    msgs = [{"role": "system", "content": "system prompt"}]
    for i in range(rounds):
        msgs.extend(_make_round(i, tool_name=tool_name, n_parallel=n_parallel))
    return msgs


def _microcompact(messages, keep_recent_rounds=3):
    """Inline copy matching loop.py logic for testing without full import chain."""
    from ouroboros.utils import estimate_tokens

    _PROTECTED = frozenset({"knowledge_read", "data_read"})
    _THRESHOLD = 100_000

    total = sum(estimate_tokens(str(m.get("content") or "")) + 4 for m in messages)
    if total < _THRESHOLD:
        return

    round_starts = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")]
    if len(round_starts) <= keep_recent_rounds:
        return

    protected_boundary = round_starts[-keep_recent_rounds]

    for i, m in enumerate(messages):
        if i >= protected_boundary:
            break
        if m.get("role") != "tool":
            continue
        if m.get("content") == _CLEARED:
            continue
        content = str(m.get("content") or "")
        if content.startswith("⚠️"):
            continue
        tid = m.get("tool_call_id", "")
        tool_name = ""
        if tid:
            for j in range(i - 1, -1, -1):
                prev = messages[j]
                if prev.get("role") != "assistant":
                    continue
                for tc in prev.get("tool_calls") or []:
                    if tc.get("id") == tid:
                        tool_name = tc.get("function", {}).get("name", "")
                break
        if tool_name in _PROTECTED:
            continue
        messages[i] = {**m, "content": _CLEARED}


def test_small_context_no_clearing():
    """Below token threshold — nothing should be cleared."""
    msgs = _build_session(10, n_parallel=1)
    # Small content, won't exceed 100k tokens
    for m in msgs:
        if m.get("role") == "tool":
            m["content"] = "small result"
    _microcompact(msgs)
    assert not any(m.get("content") == _CLEARED for m in msgs)


def test_large_context_clears_old_rounds():
    """Above threshold — old rounds cleared, last 3 kept."""
    msgs = _build_session(8)
    # Make content large enough to exceed threshold
    for m in msgs:
        if m.get("role") == "tool":
            m["content"] = "x" * 60000
    _microcompact(msgs, keep_recent_rounds=3)
    cleared = [m for m in msgs if m.get("content") == _CLEARED]
    kept = [m for m in msgs if m["role"] == "tool" and m["content"] != _CLEARED]
    assert len(cleared) == 5  # rounds 0-4
    assert len(kept) == 3  # rounds 5-7


def test_protected_tools_not_cleared():
    """knowledge_read and data_read results should never be cleared."""
    msgs = [{"role": "system", "content": "sys"}]
    # 5 rounds of knowledge_read + 5 rounds of navigator
    for i in range(5):
        msgs.extend(_make_round(i, tool_name="knowledge_read", content="x" * 60000))
    for i in range(5, 10):
        msgs.extend(_make_round(i, tool_name="gigasearch", content="x" * 60000))
    _microcompact(msgs, keep_recent_rounds=3)
    for m in msgs:
        if m["role"] == "tool":
            tid = m.get("tool_call_id", "")
            if tid.startswith(("c0_", "c1_", "c2_", "c3_", "c4_")):
                # knowledge_read rounds — should NOT be cleared
                assert m["content"] != _CLEARED, f"Protected tool result cleared: {tid}"


def test_error_results_not_cleared():
    """Tool results starting with warning emoji should be preserved."""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(8):
        content = "⚠️ ERROR: something failed" if i == 1 else "x" * 60000
        msgs.extend(_make_round(i, content=content))
    _microcompact(msgs, keep_recent_rounds=3)
    error_msg = [m for m in msgs if m["role"] == "tool" and m.get("tool_call_id", "").startswith("c1_")]
    assert error_msg[0]["content"].startswith("⚠️")


def test_parallel_calls_kept_together():
    """All tool results from a recent round should be kept, even with 8 parallel calls."""
    msgs = [{"role": "system", "content": "sys"}]
    # 4 rounds, last one has 8 parallel calls
    for i in range(3):
        msgs.extend(_make_round(i, content="x" * 60000))
    msgs.extend(_make_round(3, content="x" * 60000, n_parallel=8))
    _microcompact(msgs, keep_recent_rounds=3)
    # Round 3 (last) has 8 tool results — all should be kept
    round3_tools = [m for m in msgs if m["role"] == "tool" and m.get("tool_call_id", "").startswith("c3_")]
    assert len(round3_tools) == 8
    assert all(m["content"] != _CLEARED for m in round3_tools)


def test_assistant_messages_untouched():
    """Assistant reasoning messages should never be modified."""
    msgs = _build_session(8)
    for m in msgs:
        if m["role"] == "tool":
            m["content"] = "x" * 60000
    _microcompact(msgs, keep_recent_rounds=3)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert all("reasoning" in m["content"] for m in assistants)


def test_tool_call_id_preserved():
    """Cleared messages should keep their tool_call_id."""
    msgs = _build_session(8)
    for m in msgs:
        if m["role"] == "tool":
            m["content"] = "x" * 60000
    _microcompact(msgs, keep_recent_rounds=3)
    cleared = [m for m in msgs if m.get("content") == _CLEARED]
    assert all("tool_call_id" in m for m in cleared)


def test_idempotent():
    """Running microcompact twice should produce same result."""
    msgs = _build_session(8)
    for m in msgs:
        if m["role"] == "tool":
            m["content"] = "x" * 60000
    _microcompact(msgs, keep_recent_rounds=3)
    count1 = sum(1 for m in msgs if m.get("content") == _CLEARED)
    _microcompact(msgs, keep_recent_rounds=3)
    count2 = sum(1 for m in msgs if m.get("content") == _CLEARED)
    assert count1 == count2


def test_production_microcompact_preserves_important_schedule_task_fact():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(8):
        content = "Task request queued child123: Generate image" if i == 0 else "x" * 2000
        msgs.extend(_make_round(i, tool_name="schedule_task", content=content))

    _production_microcompact(msgs, keep_recent_rounds=3, token_threshold=0)

    old_schedule_result = next(m for m in msgs if m.get("tool_call_id") == "c0_0")
    assert old_schedule_result["content"] != _CLEARED
    assert "[Tool receipt]" in old_schedule_result["content"]
    assert "schedule_task" in old_schedule_result["content"]
    assert "queued" in old_schedule_result["content"].lower()
    assert "fact.task_id=child123" in old_schedule_result["content"]


def test_production_microcompact_replaces_huge_old_result_with_receipt():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(8):
        msgs.extend(_make_round(i, tool_name="skill_exec", content="HEAD" + ("x" * 5000) + "TAIL"))

    _production_microcompact(msgs, keep_recent_rounds=3, token_threshold=0)

    old_result = next(m for m in msgs if m.get("tool_call_id") == "c0_0")
    assert old_result["content"] != _CLEARED
    assert "[Tool receipt]" in old_result["content"]
    assert "sha256=" in old_result["content"]
    assert "full result omitted" in old_result["content"]
    assert "TAIL" not in old_result["content"]


def test_production_microcompact_does_not_expose_local_tmp_artifact_by_default(tmp_path):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(8):
        msgs.extend(_make_round(i, tool_name="skill_exec", content="HEAD" + ("x" * 50_000) + "TAIL"))

    _production_microcompact(
        msgs,
        keep_recent_rounds=3,
        token_threshold=0,
        drive_root=tmp_path,
        task_id="task1",
    )

    old_result = next(m for m in msgs if m.get("tool_call_id") == "c0_0")
    assert "artifact_ref=" not in old_result["content"]
    assert "artifact_path=" not in old_result["content"]
    assert not (tmp_path / "runtime" / "tool_artifacts" / "tmp" / "task1" / "c0_0.txt").exists()
    assert "TAIL" not in old_result["content"]
