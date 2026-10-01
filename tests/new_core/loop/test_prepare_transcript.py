"""Task 7.3: prepare_transcript compaction policy (manual / emergency / routine / none)."""

from __future__ import annotations

from new_core.loop.nodes import prepare_transcript
from tests.new_core.loop._fakes import FakeCompactor, FakeEvents, FakeLLM, make_config, make_ctx, make_state


def _prep(*, compactor, config, mutate_state=None):
    events = FakeEvents()
    ctx = make_ctx(llm=FakeLLM([]), compactor=compactor, events=events, config=config)
    state = make_state(ctx, messages=[{"role": "user", "content": "x"}] * 50)
    if mutate_state:
        mutate_state(state)
    prepare_transcript(state, ctx)
    return state, events


def test_manual_compaction_uses_pending_keep_recent() -> None:
    comp = FakeCompactor(compact_usage={"prompt_tokens": 1})
    state, events = _prep(
        compactor=comp,
        config=make_config(),
        mutate_state=lambda s: setattr(s, "pending_compaction", 30),
    )
    assert comp.compact_calls == [30]
    assert state.pending_compaction is None
    assert events.usage_calls == [("light", "compaction")]
    # manual → first microcompact uses a zero threshold
    assert comp.microcompact_calls[0] == (3, 0)


def test_emergency_compaction_by_chars() -> None:
    comp = FakeCompactor(chars=2_000_000)
    _prep(compactor=comp, config=make_config(emergency_use_chars=True, emergency_threshold=1_000_000))
    assert comp.compact_calls == [50]


def test_emergency_compaction_by_tokens() -> None:
    comp = FakeCompactor(tokens=500_000)
    _prep(compactor=comp, config=make_config(emergency_use_chars=False, emergency_threshold=150_000))
    assert comp.compact_calls == [50]


def test_routine_local_compaction_fires() -> None:
    comp = FakeCompactor()
    _prep(
        compactor=comp,
        config=make_config(),
        mutate_state=lambda s: (setattr(s, "active_use_local", True), setattr(s, "round_idx", 7)),
    )
    assert comp.compact_calls == [20]


def test_routine_suppressed_on_checkpoint_round() -> None:
    comp = FakeCompactor()
    _prep(
        compactor=comp,
        config=make_config(),
        mutate_state=lambda s: (
            setattr(s, "active_use_local", True),
            setattr(s, "round_idx", 7),
            setattr(s, "checkpoint_injected", True),
        ),
    )
    assert comp.compact_calls == []


def test_no_compaction_when_small_and_remote() -> None:
    comp = FakeCompactor()
    _prep(compactor=comp, config=make_config(), mutate_state=lambda s: setattr(s, "round_idx", 7))
    assert comp.compact_calls == []
    # ledger note + seal always run
    assert comp.upsert_calls == 1
    assert comp.seal_calls == 1


def test_manual_precedence_over_emergency() -> None:
    comp = FakeCompactor(chars=2_000_000)
    _prep(
        compactor=comp,
        config=make_config(emergency_use_chars=True, emergency_threshold=1_000_000),
        mutate_state=lambda s: setattr(s, "pending_compaction", 10),
    )
    assert comp.compact_calls == [10]
