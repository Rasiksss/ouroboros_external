from __future__ import annotations

import base64
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests
from ouroboros.storage.runtime_factory import sqlite_url_for_path
from ouroboros.storage.runtime_models import TaskEpisode, TaskRequest, ToolInvocation
from ouroboros.storage.runtime_store import RuntimeStore, init_runtime_store
from ouroboros.storage.sql_runtime_store import SqlRuntimeStore


def _record_tool_result(
    tmp_path,
    *,
    task_id: str,
    tool_name: str,
    result: str,
    tool_args: dict[str, object] | None = None,
    catalog=None,
) -> tuple[RuntimeStore, list[ToolInvocation]]:
    from new_core.tools.catalog import ToolCatalog
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata
    from tests._shared import collecting_event_sink

    store = init_runtime_store(tmp_path)
    # The per-task context is an argument now, not a field reached through the registry's
    # private ``_ctx``; only the four attributes the recorder reads have to be present.
    ctx = SimpleNamespace(
        drive_root=tmp_path,
        task_id=task_id,
        conversation_id="session-1",
        current_chat_id=1,
    )
    args = dict(tool_args or {})
    executor = build_tool_call_executor(
        catalog=catalog or ToolCatalog(), drive_logs=tmp_path / "logs", events=collecting_event_sink()
    )
    executor.process_results(
        [
            {
                "tool_call_id": f"call-{task_id}",
                "fn_name": tool_name,
                "result": result,
                "is_error": False,
                "tool_args": args,
                "args_for_log": args,
                "result_meta": {"status": "ok"},
                # Mirrors aexecute: the runtime-metadata snapshot is pinned into
                # the exec dict and carries retrieval aliases.
                "tool_metadata": resolve_tool_runtime_metadata(tool_name, catalog),
            }
        ],
        messages=[],
        trace={"tool_calls": []},
        ctx=ctx,
    )
    return store, store.list_tool_invocations(task_id=task_id)


def test_working_retrieval_storage_round_trip_uses_production_schema(tmp_path):
    store = init_runtime_store(tmp_path)
    invocation = ToolInvocation(
        tool_invocation_id="call-1",
        task_id="task-1",
        conversation_id="session-1",
        tool_name="weather_lookup",
        status="ok",
        args_redacted={"city": "Москва"},
        result_summary={"summary": "Солнечно"},
        result_index={"search_terms": ["москва", "солнечно"]},
        search_terms=["москва", "солнечно"],
        payload_hash="hash-1",
        created_at="2026-07-01T10:00:00+00:00",
        expires_at="2026-08-01T10:00:00+00:00",
    )
    episode = TaskEpisode(
        episode_id="episode-1",
        task_id="task-1",
        conversation_id="session-1",
        created_at="2026-07-01T10:01:00+00:00",
        user_text="Какая была погода в Москве?",
        answer_preview="В Москве было солнечно.",
        tools_used=["weather_lookup"],
        domains=["weather"],
        entities={"city": ["Москва"]},
        refs={"tool_invocation_ids": ["call-1"]},
        search_terms=["москва", "погода", "солнечно"],
        expires_at="2026-10-01T10:00:00+00:00",
    )

    store.record_tool_invocation(invocation)
    store.record_task_episode(episode)

    assert store.list_tool_invocations(task_id="task-1") == [invocation]
    assert store.list_task_episodes(
        conversation_id="session-1",
        search_terms=["москва"],
    ) == [episode]


def test_working_retrieval_sensitive_fields_keep_production_encryption(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv(
        "OUROBOROS_CHAT_ENCRYPTION_KEY",
        base64.b64encode(b"k" * 32).decode("ascii"),
    )
    reset_encryption_cache_for_tests()
    store = init_runtime_store(tmp_path)
    store._dialect = "postgresql"
    try:
        store.record_tool_invocation(
            ToolInvocation(
                tool_invocation_id="encrypted-call",
                args_redacted={"query": "private query"},
                result_summary={"answer": "private result"},
            )
        )
        store.record_task_episode(
            TaskEpisode(
                episode_id="encrypted-episode",
                user_text="private question",
                answer_preview="private answer",
            )
        )

        with store._lock, store._connection() as connection:
            invocation_row = connection.execute(
                text("SELECT args_redacted_json, result_summary_json, payload_json FROM tool_invocations")
            ).one()
            episode_row = connection.execute(
                text("SELECT user_text, answer_preview, payload_json FROM task_episodes")
            ).one()

        assert all(str(value).startswith("enc:v1:") for value in invocation_row)
        assert all(str(value).startswith("enc:v1:") for value in episode_row)
        assert store.list_tool_invocations()[0].args_redacted == {"query": "private query"}
        assert store.list_task_episodes()[0].user_text == "private question"
    finally:
        reset_encryption_cache_for_tests()


def test_working_retrieval_cleanup_keeps_unexpired_rows(tmp_path):
    store = init_runtime_store(tmp_path)
    store.record_tool_invocation(
        ToolInvocation(
            tool_invocation_id="expired-call",
            tool_name="calculator",
            created_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-02T00:00:00+00:00",
        )
    )
    store.record_task_episode(
        TaskEpisode(
            episode_id="active-episode",
            created_at="2026-07-01T00:00:00+00:00",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )

    removed = store.cleanup_expired_working_retrieval(now_iso="2026-07-02T00:00:00+00:00")

    assert removed == {"task_episodes": 0, "tool_invocations": 1}
    assert store.list_tool_invocations() == []
    assert [item.episode_id for item in store.list_task_episodes()] == ["active-episode"]


def test_universal_index_handles_unknown_tools_and_redacts_secrets():
    from ouroboros.agent.retrieval_index import build_retrieval_index

    index = build_retrieval_index(
        "custom_extension.lookup",
        {
            "project": "Orion",
            "api_token": "top-secret-token",
            "document_id": "DOC-42",
        },
        {
            "status": "ok",
            "title": "План проекта Orion",
            "url": "https://example.test/docs/DOC-42",
        },
    )

    serialized = index.model_dump_json()
    assert "custom_extension.lookup" in index.terms
    assert "orion" in index.terms
    assert "top-secret-token" not in serialized
    assert index.refs["document_id"] == "DOC-42"
    assert index.refs["url"] == "https://example.test/docs/DOC-42"


def test_universal_index_bounds_arbitrary_list_results():
    from ouroboros.agent.retrieval_index import build_retrieval_index

    index = build_retrieval_index(
        "custom_extension.lookup",
        {},
        ["x" * 4000 for _ in range(100)],
    )

    assert len(index.model_dump_json()) < 10_000


def test_tool_invocation_builder_preserves_schema_without_raw_result():
    from ouroboros.agent.working_retrieval import build_tool_invocation

    invocation = build_tool_invocation(
        {
            "tool_call_id": "call-7",
            "fn_name": "custom_extension.lookup",
            "args_for_log": {"project": "Orion"},
            "result": '{"status":"ok","document_id":"DOC-42"}',
            "result_meta": {"status": "ok"},
            "is_error": False,
        },
        task_id="task-7",
        conversation_id="session-1",
        created_at="2026-07-01T10:00:00+00:00",
        retention_days=30,
    )

    assert invocation.tool_invocation_id == "call-7"
    assert invocation.tool_name == "custom_extension.lookup"
    assert invocation.payload == {"stored_raw": False}
    assert invocation.result_index["refs"]["document_id"] == "DOC-42"
    assert invocation.expires_at == "2026-07-31T10:00:00+00:00"


def test_tool_invocation_builder_redacts_unknown_tool_secret_fields():
    from ouroboros.agent.working_retrieval import build_tool_invocation

    invocation = build_tool_invocation(
        {
            "fn_name": "custom_extension.lookup",
            "args_for_log": {
                "query": "Orion",
                "api_token": "top-secret-token",
                "nested": {"authorization_header": "Bearer hidden"},
            },
            "result": "ok",
        },
        task_id="task-secret",
    )

    serialized = invocation.model_dump_json()
    assert "top-secret-token" not in serialized
    assert "Bearer hidden" not in serialized


def test_retrieval_gate_remains_domain_agnostic():
    from ouroboros.agent.working_retrieval import retrieval_requested

    assert retrieval_requested("мы сравнивали две организации вчера: ромашку и петрушку")
    assert retrieval_requested("вернись к прошлому результату")
    assert not retrieval_requested("рассчитай 2 + 2")


def test_retrieval_returns_relevant_prior_episode_only_for_continuity_query(tmp_path):
    from ouroboros.agent.working_retrieval import build_retrieval_section

    store = init_runtime_store(tmp_path)
    store.record_task_episode(
        TaskEpisode(
            episode_id="weather",
            task_id="task-weather",
            conversation_id="session-1",
            created_at="2026-07-01T10:00:00+00:00",
            user_text="Какая погода была в Москве?",
            answer_preview="В Москве было солнечно.",
            search_terms=["погода", "москва", "солнечно"],
        )
    )
    store.record_task_episode(
        TaskEpisode(
            episode_id="other-session",
            task_id="task-private",
            conversation_id="session-2",
            created_at="2026-07-02T10:00:00+00:00",
            user_text="Секретный проект",
            answer_preview="Не должен попасть в контекст.",
            search_terms=["проект"],
        )
    )

    section = build_retrieval_section(
        store,
        "Что мы раньше выяснили про погоду в Москве?",
        conversation_id="session-1",
    )
    unrelated = build_retrieval_section(
        store,
        "Рассчитай 2 + 2",
        conversation_id="session-1",
    )

    assert "task-weather" in section
    assert "В Москве было солнечно" in section
    assert "Секретный проект" not in section
    assert unrelated == ""


def test_retrieval_respects_token_budget_for_first_card(tmp_path):
    from ouroboros.agent.working_retrieval import build_retrieval_section

    store = init_runtime_store(tmp_path)
    store.record_task_episode(
        TaskEpisode(
            episode_id="large-card",
            task_id="task-large",
            conversation_id="session-1",
            user_text="Погода в Москве",
            answer_preview="Солнечно " * 100,
            search_terms=["погод", "москв"],
        )
    )

    section = build_retrieval_section(
        store,
        "Что мы раньше выяснили про погоду в Москве?",
        conversation_id="session-1",
        token_budget=1,
    )

    assert section == ""


def test_retrieval_skips_legacy_failed_task_episode(tmp_path):
    from ouroboros.agent.working_retrieval import build_retrieval_section

    store = init_runtime_store(tmp_path)
    store.create_task(
        TaskRequest(
            id="failed-task",
            text="Сравни Ромашку и Петрушку",
            conversation_id="session-1",
        ),
        status="failed",
    )
    store.record_task_episode(
        TaskEpisode(
            episode_id="failed-episode",
            task_id="failed-task",
            conversation_id="session-1",
            user_text="Сравни Ромашку и Петрушку",
            answer_preview="Ошибка.",
            search_terms=["ромашк", "петрушк"],
        )
    )

    section = build_retrieval_section(
        store,
        "Что мы раньше выяснили про Ромашку и Петрушку?",
        conversation_id="session-1",
    )

    assert section == ""


def test_task_episode_builder_is_tool_agnostic():
    from ouroboros.agent.working_retrieval import build_task_episode

    episode = build_task_episode(
        {
            "id": "task-9",
            "text": "Найди документ Orion",
            "conversation_id": "session-1",
        },
        "Нашёл документ DOC-42.",
        {
            "tool_calls": [
                {
                    "tool": "custom_extension.lookup",
                    "args": {"project": "Orion"},
                    "result": "DOC-42",
                }
            ]
        },
        created_at="2026-07-01T10:00:00+00:00",
        retention_days=90,
    )

    assert episode is not None
    assert episode.tools_used == ["custom_extension.lookup"]
    assert "orion" in episode.search_terms
    assert episode.expires_at == "2026-09-29T10:00:00+00:00"


def test_stored_episode_merges_universal_invocation_index(tmp_path):
    from ouroboros.agent.working_retrieval import store_task_episode

    store = init_runtime_store(tmp_path)
    store.record_tool_invocation(
        ToolInvocation(
            tool_invocation_id="call-indexed",
            task_id="task-indexed",
            tool_name="custom.lookup",
            result_index={
                "entities": {"projects": ["Orion"]},
                "refs": {"document_id": "DOC-42"},
                "search_terms": ["orion", "doc-42"],
            },
            search_terms=["orion", "doc-42"],
        )
    )

    episode = store_task_episode(
        store,
        {"id": "task-indexed", "text": "Найди Orion"},
        "Нашёл DOC-42",
        {"tool_calls": []},
    )

    assert episode is not None
    assert episode.entities == {"projects": ["Orion"]}
    assert episode.refs["document_id"] == "DOC-42"
    assert episode.refs["tool_invocation_ids"] == ["call-indexed"]


def test_tool_result_processing_records_invocation(tmp_path):
    _, records = _record_tool_result(
        tmp_path,
        task_id="task-10",
        tool_name="custom.lookup",
        result='{"status":"ok","document_id":"DOC-10"}',
        tool_args={"query": "Orion"},
    )

    assert len(records) == 1
    assert records[0].tool_name == "custom.lookup"
    assert records[0].result_index["refs"]["document_id"] == "DOC-10"


def test_mcp_retrieval_aliases_make_neutral_result_discoverable(tmp_path, monkeypatch):
    from new_core.tools.providers import McpToolProvider
    from ouroboros.agent.working_retrieval import build_retrieval_section, store_task_episode
    from ouroboros.mcp.manager import mcp_manager
    from tests._shared import make_catalog

    tool_name = "mcp__hr-provider__mcp_query"
    monkeypatch.setattr(
        mcp_manager,
        "retrieval_aliases",
        lambda name: ("pulse", "пульс", "pulse_people", "heimdall") if name == tool_name else (),
    )
    monkeypatch.setattr(mcp_manager, "is_available", lambda: True)
    monkeypatch.setattr(mcp_manager, "resolve", lambda name: ("hr-provider", "mcp_query"))
    monkeypatch.setattr(mcp_manager, "discovered_tools", list)
    store, records = _record_tool_result(
        tmp_path,
        task_id="task-pulse",
        tool_name=tool_name,
        result='{"employee":"Иванов","grade":"G12"}',
        tool_args={"employee": "Иванов"},
        catalog=make_catalog([McpToolProvider()]),
    )

    # Aliases are tokenized into search terms ("pulse_people" -> "pulse", "people").
    assert records[0].search_terms[:4] == ["pulse", "пульс", "people", "heimdall"]
    # The stable tool name rides along with the declared aliases.
    assert records[0].result_index["retrieval_aliases"] == [
        "pulse",
        "пульс",
        "pulse_people",
        "heimdall",
        tool_name,
    ]

    episode = store_task_episode(
        store,
        {
            "id": "task-pulse",
            "text": " ".join(f"question-term-{index}" for index in range(100)),
            "conversation_id": "session-1",
        },
        " ".join(f"answer-term-{index}" for index in range(100)),
        {"tool_calls": []},
    )

    assert episode is not None
    assert len(episode.search_terms) == 120
    assert episode.search_terms[:4] == ["pulse", "пульс", "pulse_people", "heimdall"]
    assert "answer-term-99" not in episode.search_terms

    section = build_retrieval_section(
        store,
        "Что мы раньше смотрели в Пульсе?",
        conversation_id="session-1",
    )
    assert "task-pulse" in section


def test_mcp_alias_lookup_failure_still_records_invocation(tmp_path, monkeypatch):
    from ouroboros.mcp.manager import mcp_manager

    def _raise_alias_error(_name):
        raise RuntimeError("alias lookup failed")

    monkeypatch.setattr(mcp_manager, "retrieval_aliases", _raise_alias_error)
    _, records = _record_tool_result(
        tmp_path,
        task_id="task-alias-error",
        tool_name="mcp__hr-provider__mcp_query",
        result='{"employee":"Иванов"}',
    )

    assert len(records) == 1
    assert records[0].tool_name == "mcp__hr-provider__mcp_query"


def test_task_pipeline_records_completed_episode(tmp_path):
    from ouroboros.agent.task_pipeline import _record_working_task_episode

    store = init_runtime_store(tmp_path)
    _record_working_task_episode(
        SimpleNamespace(drive_root=tmp_path),
        {
            "id": "task-11",
            "text": "Найди Orion",
            "conversation_id": "session-1",
        },
        "Нашёл DOC-11",
        {"tool_calls": []},
    )

    episodes = store.list_task_episodes(conversation_id="session-1")
    assert len(episodes) == 1
    assert episodes[0].task_id == "task-11"


def test_supervisor_cleanup_records_checkpoint(tmp_path, monkeypatch):
    from supervisor.runtime import _maybe_cleanup_working_retrieval

    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS", "24")
    store = init_runtime_store(tmp_path)
    store.record_tool_invocation(
        ToolInvocation(
            tool_invocation_id="expired-supervisor-call",
            tool_name="custom.lookup",
            created_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-02T00:00:00+00:00",
        )
    )

    _maybe_cleanup_working_retrieval(
        SimpleNamespace(store=store),
        now_iso="2026-07-01T00:00:00+00:00",
    )

    assert store.list_tool_invocations() == []
    state = store.load_owner_runtime_state()
    assert state is not None
    assert state["working_retrieval_cleanup"]["removed"]["tool_invocations"] == 1


@pytest.fixture(autouse=True)
def _clear_encryption_cache():
    reset_encryption_cache_for_tests()
    yield
    reset_encryption_cache_for_tests()


@pytest.fixture
def encryption_env(monkeypatch) -> None:
    key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", key)
    reset_encryption_cache_for_tests()


def test_tool_invocation_and_episode_round_trip_plaintext(tmp_path) -> None:
    store = SqlRuntimeStore(create_engine(sqlite_url_for_path(tmp_path / "runtime.sqlite3")), db_url="")

    tool = store.record_tool_invocation(
        ToolInvocation(
            tool_invocation_id="tool-1",
            task_id="task-1",
            conversation_id="dialogs:1",
            tool_name="pulse_people_search",
            status="ok",
            args_redacted={"mode": "query"},
            result_summary={"model": "Финансы группы.PL", "row_count": 2},
            result_index={"model": "Финансы группы.PL", "row_count": 2},
            search_terms=["pulse", "чистая прибыль"],
            payload={"stored_raw": False},
            created_at="2026-06-30T09:00:00+00:00",
        )
    )
    episode = store.record_task_episode(
        TaskEpisode(
            episode_id="episode-1",
            task_id="task-1",
            conversation_id="dialogs:1",
            user_text="какие общие результаты по всем банкам России?",
            answer_preview="Сравнение по чистой прибыли подготовлено.",
            tools_used=["pulse_people_search"],
            domains=["pulse"],
            entities={"metrics": ["Чистая прибыль / убыток"]},
            refs={"tool_invocation_ids": [tool.tool_invocation_id]},
            search_terms=["pulse", "чистая прибыль", "тб"],
            created_at="2026-06-30T09:01:00+00:00",
        )
    )

    loaded_tool = store.list_tool_invocations(task_id="task-1")[0]
    loaded_episode = store.list_task_episodes(conversation_id="dialogs:1")[0]

    assert loaded_tool.result_summary["row_count"] == 2
    assert loaded_tool.args_redacted == {"mode": "query"}
    assert loaded_episode.episode_id == episode.episode_id
    assert loaded_episode.domains == ["pulse"]
    assert "Чистая прибыль / убыток" in loaded_episode.entities["metrics"]


def test_working_retrieval_sensitive_fields_encrypt_on_postgres_dialect(tmp_path, encryption_env) -> None:
    store = SqlRuntimeStore(create_engine(sqlite_url_for_path(tmp_path / "runtime.sqlite3")), db_url="")
    store._dialect = "postgresql"

    store.record_task_episode(
        TaskEpisode(
            episode_id="episode-enc",
            task_id="task-enc",
            conversation_id="dialogs:1",
            user_text="секретный запрос",
            answer_preview="секретный ответ",
            tools_used=["send_assignment"],
            domains=["assignment"],
            search_terms=["assignment"],
            created_at="2026-06-30T09:01:00+00:00",
        )
    )
    store.record_tool_invocation(
        ToolInvocation(
            tool_invocation_id="tool-enc",
            task_id="task-enc",
            conversation_id="dialogs:1",
            tool_name="send_assignment",
            status="ok",
            args_redacted={"subject": "[OMITTED]"},
            result_summary={"assignment_id": "abc"},
            search_terms=["assignment"],
            created_at="2026-06-30T09:01:00+00:00",
        )
    )

    with store._lock, store._connection() as conn:
        episode_row = conn.execute(text("SELECT user_text, answer_preview, payload_json FROM task_episodes")).fetchone()
        tool_row = conn.execute(
            text("SELECT args_redacted_json, result_summary_json, payload_json FROM tool_invocations")
        ).fetchone()

    assert episode_row is not None
    assert str(episode_row.user_text).startswith("enc:v1:")
    assert str(episode_row.answer_preview).startswith("enc:v1:")
    assert str(episode_row.payload_json).startswith("enc:v1:")
    assert tool_row is not None
    assert str(tool_row.args_redacted_json).startswith("enc:v1:")
    assert str(tool_row.result_summary_json).startswith("enc:v1:")
    assert str(tool_row.payload_json).startswith("enc:v1:")

    assert store.list_task_episodes()[0].user_text == "секретный запрос"
    assert store.list_tool_invocations()[0].result_summary == {"assignment_id": "abc"}


def test_retrieval_gate_is_domain_agnostic() -> None:
    from ouroboros.agent.working_retrieval import retrieval_requested

    assert retrieval_requested("мы сравнивали две организации вчера: ромашку и петрушку")
    assert retrieval_requested("вернись к прошлому поручению")
    assert not retrieval_requested("данные каких ТБ у тебя есть?")
    assert not retrieval_requested("создай встречу на сегодня")
    assert not retrieval_requested("посмотри PL по всем банкам")


def test_emit_task_results_skips_working_episode_for_failed_task(monkeypatch, tmp_path) -> None:
    from ouroboros.agent import task_pipeline

    recorded: list[str] = []
    distilled: list[str] = []

    monkeypatch.setattr(task_pipeline, "_store_task_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_pipeline, "_task_ended_unsuccessfully", lambda _env, _task_id: True)
    monkeypatch.setattr(
        task_pipeline, "_record_working_task_episode", lambda *args, **kwargs: recorded.append("episode")
    )
    monkeypatch.setattr(
        task_pipeline, "_enqueue_memory_distillation", lambda *args, **kwargs: distilled.append("memory")
    )
    monkeypatch.setattr(task_pipeline, "record_event", lambda *args, **kwargs: None)

    env = SimpleNamespace(drive_root=tmp_path)
    task_pipeline.emit_task_results(
        env,
        llm=object(),
        pending_events=[],
        task={"id": "failed-task", "type": "task", "chat_id": 1, "text": "do it"},
        text="failed",
        usage={},
        llm_trace={"tool_calls": []},
        start_time=time.time(),
        drive_logs=tmp_path / "logs",
        ctx=SimpleNamespace(pending_restart_reason=""),
    )

    assert recorded == []
    assert distilled == []


def test_cleanup_expired_working_retrieval_rows(tmp_path) -> None:
    store = SqlRuntimeStore(create_engine(sqlite_url_for_path(tmp_path / "runtime.sqlite3")), db_url="")
    store.record_task_episode(
        TaskEpisode(
            episode_id="expired-episode",
            task_id="task-1",
            conversation_id="dialogs:1",
            user_text="old",
            answer_preview="old",
            expires_at="2020-01-01T00:00:00+00:00",
        )
    )
    store.record_tool_invocation(
        ToolInvocation(
            tool_invocation_id="expired-tool",
            task_id="task-1",
            conversation_id="dialogs:1",
            tool_name="send_assignment",
            status="ok",
            expires_at="2020-01-01T00:00:00+00:00",
        )
    )

    removed = store.cleanup_expired_working_retrieval(now_iso="2026-01-01T00:00:00+00:00", limit=100)

    assert removed == {"task_episodes": 1, "tool_invocations": 1}
    assert store.list_task_episodes() == []
    assert store.list_tool_invocations() == []
