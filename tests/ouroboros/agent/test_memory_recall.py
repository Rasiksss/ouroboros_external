"""Tests for ouroboros.agent.memory_recall: relevance-based memory selection."""

from __future__ import annotations

import logging
import threading

from ouroboros.agent.memory_recall import recall_relevant_memories
from ouroboros.agent_events.interfaces import LlmUsageReported
from ouroboros.agent_runtime.cancellation import EventCancellation
from ouroboros.storage.core_memory.memory import MemoryFactory
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.factories import make_mock_llm
from tests.helpers.shared import TEST_SCOPE


def _store(tmp_path):
    """Unified memory for the test's configured drive root."""
    del tmp_path
    return MemoryFactory().get_memory()


def _patch_row(store, name, **fields):
    """Rewrite stored row fields (timestamps/status) that the save API owns."""
    row = store._repo.fetch(TEST_SCOPE, name)
    assert row is not None
    for key, value in fields.items():
        setattr(row, key, value)
    store._repo.upsert(TEST_SCOPE, row)


def _make_store_with_topics(tmp_path, n, prefix="topic"):
    store = _store(tmp_path)
    for i in range(n):
        store.save(TEST_SCOPE, f"{prefix}_{i}", "project", f"desc {i}", f"content {i}")
    return store


def test_recall_membership_follows_the_kind_policy():
    """The two type sets are the policy table read back, not a literal list here."""
    from ouroboros.agent.memory_recall import _AUTO_KINDS, _SELECTOR_KINDS
    from ouroboros.storage.core_memory.memory.domain.models import KIND_POLICIES

    assert {k for k, p in KIND_POLICIES.items() if p.injection == "recall_auto"} == _AUTO_KINDS
    assert {k for k, p in KIND_POLICIES.items() if p.injection == "recall_selector"} == _SELECTOR_KINDS
    # Knowledge bodies run to 80 000 chars; recall truncates at 3 000, so it is
    # deliberately in neither set.
    assert "knowledge" not in _AUTO_KINDS | _SELECTOR_KINDS
    assert "identity" not in _AUTO_KINDS | _SELECTOR_KINDS


def test_knowledge_memories_are_never_recalled(tmp_path):
    store = _store(tmp_path)
    store.save(TEST_SCOPE, "git-recipes", "knowledge", "Rebase workflow", "knowledge body")
    llm = make_mock_llm()
    llm.chat.return_value = ({"content": '{"selected": ["git-recipes"]}'}, {"prompt_tokens": 10})

    result = recall_relevant_memories("git recipes", store, TEST_SCOPE, llm)

    assert result == []
    llm.chat.assert_not_called()


def test_empty_store_returns_empty_list(tmp_path):
    store = _store(tmp_path)
    llm = make_mock_llm()

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert result == []
    llm.chat.assert_not_called()


def test_recall_uses_selector_for_project_reference_topics(tmp_path):
    store = _make_store_with_topics(tmp_path, 5)
    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["topic_2"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert len(result) == 1
    assert "content 2" in result[0].content
    assert llm.chat.call_args.kwargs["timeout_sec"] == 8


def test_stop_ends_memory_recall_wait_without_using_its_late_selection(tmp_path):
    """Recall observes the run stop while its selector call is still in flight."""
    store = _make_store_with_topics(tmp_path, 1)
    cancellation = EventCancellation()
    entered = threading.Event()
    release = threading.Event()
    result: list[object] = []

    class BlockingLLM:
        def chat(self, **kwargs):
            assert callable(kwargs["stop_requested"])
            entered.set()
            assert release.wait(5)
            return {"content": '{"selected": ["topic_0"]}'}, {}

    thread = threading.Thread(
        target=lambda: result.extend(
            recall_relevant_memories("query", store, TEST_SCOPE, BlockingLLM(), cancellation=cancellation)
        )
    )
    thread.start()
    assert entered.wait(2)
    try:
        cancellation.request_stop()
        thread.join(2)
        assert not thread.is_alive(), "recall kept waiting for a stopped task's selector"
        assert result == []
    finally:
        release.set()
        thread.join(2)


def test_recall_auto_includes_user_feedback_and_selects_project_reference(tmp_path):
    store = _store(tmp_path)
    store.save(TEST_SCOPE, "project_new", "project", "new project", "project content")
    store.save(TEST_SCOPE, "reference_new", "reference", "new reference", "reference content")
    store.save(TEST_SCOPE, "feedback_old", "feedback", "old feedback", "feedback content")
    store.save(TEST_SCOPE, "user_old", "user", "old user", "user content")

    for name in ("user_old", "feedback_old"):
        _patch_row(store, name, updated_at=1.0)
    for name in ("project_new", "reference_new"):
        _patch_row(store, name, updated_at=999.0)

    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["project_new"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm, max_results=3)

    contents = [memory.content for memory in result]
    assert len(contents) == 3
    assert "user content" in contents[0]
    assert "feedback content" in contents[1]
    assert "project content" in contents[2]
    manifest_text = llm.chat.call_args.kwargs["messages"][1]["content"]
    assert "project_new" in manifest_text
    assert "reference_new" in manifest_text
    assert "user_old" not in manifest_text
    assert "feedback_old" not in manifest_text


def test_user_feedback_do_not_exhaust_project_reference_selector_budget(tmp_path):
    store = _store(tmp_path)
    for i in range(10):
        store.save(TEST_SCOPE, f"user_{i}", "user", f"user desc {i}", f"user content {i}")
    store.save(TEST_SCOPE, "billing_project", "project", "billing migration", "billing content")

    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["billing_project"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("billing migration status", store, TEST_SCOPE, llm, max_results=5)

    contents = [memory.content for memory in result]
    assert any("billing content" in content for content in contents)
    assert sum("user content" in content for content in contents) <= 2


def test_recall_selector_candidates_include_old_query_matching_project(tmp_path):
    store = _store(tmp_path)
    for i in range(30):
        store.save(TEST_SCOPE, f"recent_project_{i}", "project", f"recent unrelated topic {i}", f"recent content {i}")
    store.save(TEST_SCOPE, "legacy_billing_fact", "project", "billing migration contract", "legacy billing content")

    _patch_row(store, "legacy_billing_fact", updated_at=1.0)
    for i in range(30):
        _patch_row(store, f"recent_project_{i}", updated_at=1_000.0 + i)

    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["legacy_billing_fact"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("What did we decide about billing migration?", store, TEST_SCOPE, llm)

    assert len(result) == 1
    assert "legacy billing content" in result[0].content
    manifest_text = llm.chat.call_args.kwargs["messages"][1]["content"]
    assert "legacy_billing_fact" in manifest_text


def test_opt_in_llm_selector_can_select_specific_topics(tmp_path):
    store = _make_store_with_topics(tmp_path, 5)
    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["topic_2"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm, use_llm_selector=True)

    assert len(result) == 1
    assert "content 2" in result[0].content
    assert llm.chat.call_args.kwargs["timeout_sec"] == 8


def test_use_llm_selector_false_disables_project_reference_selector(tmp_path):
    store = _make_store_with_topics(tmp_path, 5)
    llm = make_mock_llm()

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm, use_llm_selector=False)

    assert result == []
    llm.chat.assert_not_called()


def test_selector_failure_skips_project_reference_bodies(tmp_path):
    store = _make_store_with_topics(tmp_path, 10)
    llm = make_mock_llm()
    llm.chat.side_effect = RuntimeError("boom")

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert result == []


def test_selector_failure_still_includes_user_feedback(tmp_path):
    store = _store(tmp_path)
    store.save(TEST_SCOPE, "user_role", "user", "user role", "user content")
    store.save(TEST_SCOPE, "project_new", "project", "new project", "project content")
    llm = make_mock_llm()
    llm.chat.side_effect = RuntimeError("boom")

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert len(result) == 1
    assert "user content" in result[0].content


def test_llm_unparseable_text_skips_project_reference_bodies(tmp_path):
    store = _make_store_with_topics(tmp_path, 10)
    llm = make_mock_llm()
    llm.chat.return_value = ({"content": "not json"}, {"prompt_tokens": 10, "completion_tokens": 5})

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert result == []


def test_llm_empty_selection_returns_empty_list(tmp_path):
    store = _make_store_with_topics(tmp_path, 10)
    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": []}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm, use_llm_selector=True)

    assert result == []


def test_llm_selection_with_only_unknown_names_returns_empty_list(tmp_path):
    store = _make_store_with_topics(tmp_path, 10)
    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["does_not_exist"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm, use_llm_selector=True)

    assert result == []


def test_llm_selection_filters_unknown_names_and_returns_known(tmp_path):
    store = _make_store_with_topics(tmp_path, 10)
    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["topic_0", "does_not_exist", "topic_3"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm, max_results=5, use_llm_selector=True)

    contents = [memory.content for memory in result]
    assert len(result) == 2
    assert any("content 0" in c for c in contents)
    assert any("content 3" in c for c in contents)


def test_recall_ignores_archived_topics(tmp_path):
    """Archived rows never reach the selector — the store excludes them from the listing.

    ``superseded`` was the other status this covered. The table now refuses it
    (``ck_memory_status``); the migration maps it to ``archived`` on the way in,
    which ``test_memory_db`` covers.
    """
    store = _store(tmp_path)
    store.save(TEST_SCOPE, "active_topic", "project", "active desc", "active content")
    store.save(TEST_SCOPE, "archived_topic", "project", "archived desc", "archived content")

    _patch_row(store, "archived_topic", status="archived")

    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["active_topic", "archived_topic"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert len(result) == 1
    assert "active content" in result[0].content
    manifest_text = llm.chat.call_args.kwargs["messages"][1]["content"]
    assert "active_topic" in manifest_text
    assert "archived_topic" not in manifest_text


def test_recall_logs_unreadable_fact_and_marks_only_inserted_facts(tmp_path, caplog, monkeypatch):
    store = _store(tmp_path)
    store.save(TEST_SCOPE, "present_topic", "project", "present desc", "present content")
    store.save(TEST_SCOPE, "missing_topic", "project", "missing desc", "missing content")

    original_fetch = type(store._repo).fetch

    def _explode(self, owner_id, name):
        if name == "missing_topic":
            raise OSError("row unreadable")
        return original_fetch(self, owner_id, name)

    monkeypatch.setattr(type(store._repo), "fetch", _explode)

    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["present_topic", "missing_topic"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    caplog.set_level(logging.WARNING, logger="ouroboros.agent.memory_recall")
    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert len(result) == 1
    assert "present content" in result[0].content
    assert "missing content" not in result[0].content
    assert "Failed to read recalled memory" in caplog.text

    counts = {topic.name: topic.recall_count for topic in store.list_headers(TEST_SCOPE)}
    assert counts["present_topic"] == 1
    assert counts["missing_topic"] == 0


def test_recall_applies_hard_content_budget_with_omission_note(tmp_path):
    store = _store(tmp_path)
    store.save(TEST_SCOPE, "huge_topic", "project", "huge desc", "A" * 25_000)

    llm = make_mock_llm()
    llm.chat.return_value = (
        {"content": '{"selected": ["huge_topic"]}'},
        {"prompt_tokens": 10, "completion_tokens": 5},
    )

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm, max_results=5)

    assert len(result) == 1
    assert len(result[0].content.encode("utf-8")) <= 20_000
    assert "omitted" in result[0].content.lower()


def test_llm_selection_records_usage(tmp_path, monkeypatch):
    store = _make_store_with_topics(tmp_path, 10)
    usage = {"prompt_tokens": 123, "completion_tokens": 45}
    llm = make_mock_llm()
    llm.chat.return_value = ({"content": '{"selected": ["topic_1"]}'}, usage)

    recorded: list[dict] = []
    monkeypatch.setattr("supervisor.state.update_usage_from_llm", lambda u, **_kwargs: recorded.append(u))

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert len(result) == 1
    assert recorded == [usage]


def test_opt_in_llm_selection_reports_usage_when_a_publisher_is_present(tmp_path, monkeypatch):
    store = _make_store_with_topics(tmp_path, 10)
    usage = {
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "provider": "cloudru",
        "resolved_model": "cloudru-model",
    }
    llm = make_mock_llm()
    llm.chat.return_value = ({"content": '{"selected": ["topic_1"]}'}, usage)
    events = RecordingAgentEvents()

    called = []
    monkeypatch.setattr("supervisor.state.update_usage_from_llm", lambda u, **_kwargs: called.append(u))

    result = recall_relevant_memories(
        "query",
        store,
        TEST_SCOPE,
        llm,
        events=events,
        task_id="task-recall",
    )

    assert len(result) == 1
    assert called == []
    event = events.published[0]
    assert isinstance(event, LlmUsageReported)
    assert event.task_id == "task-recall"
    assert event.category == "memory_recall"
    assert event.source == "memory_recall"
    assert event.prompt_tokens == 123
    assert event.provider == "cloudru"
    assert event.model == "cloudru-model"


def test_llm_selection_records_usage_even_when_text_unparseable(tmp_path, monkeypatch):
    store = _make_store_with_topics(tmp_path, 10)
    usage = {"prompt_tokens": 50, "completion_tokens": 10}
    llm = make_mock_llm()
    llm.chat.return_value = ({"content": "garbage, not json"}, usage)

    recorded: list[dict] = []
    monkeypatch.setattr("supervisor.state.update_usage_from_llm", lambda u, **_kwargs: recorded.append(u))

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert result == []
    assert recorded == [usage]


def test_llm_selection_no_usage_does_not_call_update(tmp_path, monkeypatch):
    store = _make_store_with_topics(tmp_path, 10)
    llm = make_mock_llm()
    llm.chat.return_value = ({"content": '{"selected": ["topic_1"]}'}, None)

    called = []
    monkeypatch.setattr("supervisor.state.update_usage_from_llm", lambda u, **_kwargs: called.append(u))

    result = recall_relevant_memories("query", store, TEST_SCOPE, llm)

    assert len(result) == 1
    assert called == []
