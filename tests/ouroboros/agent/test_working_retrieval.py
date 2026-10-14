from __future__ import annotations

import base64
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests
from ouroboros.storage.runtime_models import TaskEpisode, TaskRequest, ToolInvocation
from ouroboros.storage.runtime_persistence import RuntimePersistence, runtime_persistence
from supervisor.infra.repositories.working_retrieval.repo import SqlWorkingRetrievalRepository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.cleanup_checkpoint import sql_cleanup_checkpoint_repository
from tests.helpers.conversations import make_sql_runtime_persistence, sql_conversation_repository
from tests.helpers.egress_auth import null_egress_auth_dependencies
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.sql_faults import fail_sql
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.user_settings import StubMcpServerIdsReader


def _record_tool_result(
    tmp_path,
    *,
    task_id: str,
    tool_name: str,
    result: str,
    tool_args: dict[str, object] | None = None,
    catalog=None,
    execution_memory_repository: IExecutionMemoryRepository | None = None,
) -> tuple[RuntimePersistence, list[ToolInvocation]]:
    from new_core.tools.catalog import ToolCatalog
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata
    from tests.helpers.shared import collecting_event_sink

    store = runtime_persistence.initialize()
    # The per-task context is an argument now, not a field reached through the registry's
    # private ``_ctx``; only the four attributes the recorder reads have to be present.
    ctx = SimpleNamespace(
        drive_root=tmp_path,
        task_id=task_id,
        scope=OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="1"),
    )
    invocations = (
        execution_memory_repository if execution_memory_repository is not None else sql_execution_memory_repository()
    )
    args = dict(tool_args or {})
    executor = build_tool_call_executor(
        catalog=catalog or ToolCatalog(),
        events=collecting_event_sink(),
        confirmation_gate=False,
        execution_memory_repository=invocations,
        audit_event_repository=sql_audit_event_repository(),
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
                "tool_metadata": resolve_tool_runtime_metadata(tool_name, catalog, scope=TEST_SCOPE),
            }
        ],
        messages=[],
        trace={"tool_calls": []},
        ctx=ctx,
    )
    return store, invocations.list_tool_invocations(task_id=task_id)


def test_working_retrieval_storage_round_trip_uses_production_schema(tmp_path):
    store = runtime_persistence.initialize()
    invocation = ToolInvocation(
        **TEST_SCOPE.to_dict(),
        tool_invocation_id="call-1",
        task_id="task-1",
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
        **TEST_SCOPE.to_dict(),
        episode_id="episode-1",
        task_id="task-1",
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

    sql_execution_memory_repository(store.engine).record_tool_invocation(invocation)
    sql_execution_memory_repository(store.engine).record_task_episode(episode)

    assert sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-1") == [invocation]
    assert sql_execution_memory_repository(store.engine).list_task_episodes(
        scope=TEST_SCOPE, search_terms=["москва"]
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
    store = runtime_persistence.initialize()
    try:
        sql_execution_memory_repository(store.engine).record_tool_invocation(
            ToolInvocation(
                **TEST_SCOPE.to_dict(),
                tool_invocation_id="encrypted-call",
                task_id="task-1",
                args_redacted={"query": "private query"},
                result_summary={"answer": "private result"},
            )
        )
        sql_execution_memory_repository(store.engine).record_task_episode(
            TaskEpisode(
                **TEST_SCOPE.to_dict(),
                episode_id="encrypted-episode",
                user_text="private question",
                answer_preview="private answer",
            )
        )

        with store.engine.connect() as connection:
            invocation_row = connection.execute(
                text("SELECT args_redacted_json, result_summary_json, payload_json FROM tool_invocations")
            ).one()
            episode_row = connection.execute(
                text("SELECT user_text, answer_preview, payload_json FROM task_episodes")
            ).one()

        assert all(str(value).startswith("enc:v1:") for value in invocation_row)
        assert all(str(value).startswith("enc:v1:") for value in episode_row)
        assert sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-1")[
            0
        ].args_redacted == {"query": "private query"}
        assert (
            sql_execution_memory_repository(store.engine).list_task_episodes(scope=TEST_SCOPE)[0].user_text
            == "private question"
        )
    finally:
        reset_encryption_cache_for_tests()


def test_working_retrieval_cleanup_keeps_unexpired_rows(tmp_path):
    store = runtime_persistence.initialize()
    sql_execution_memory_repository(store.engine).record_tool_invocation(
        ToolInvocation(
            **TEST_SCOPE.to_dict(),
            tool_invocation_id="expired-call",
            task_id="task-1",
            tool_name="calculator",
            created_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-02T00:00:00+00:00",
        )
    )
    sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(
            **TEST_SCOPE.to_dict(),
            episode_id="active-episode",
            created_at="2026-07-01T00:00:00+00:00",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )

    removed = SqlWorkingRetrievalRepository(
        store.engine, scratchpad=SqlScratchpad(store.engine)
    ).cleanup_expired_working_retrieval(now_iso="2026-07-02T00:00:00+00:00")

    assert removed == {"task_episodes": 0, "tool_invocations": 1, "scratchpad_blocks": 0}
    assert sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-1") == []
    assert [
        item.episode_id for item in sql_execution_memory_repository(store.engine).list_task_episodes(scope=TEST_SCOPE)
    ] == ["active-episode"]


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
        scope=TEST_SCOPE,
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
        scope=TEST_SCOPE,
    )

    serialized = invocation.model_dump_json()
    assert "top-secret-token" not in serialized
    assert "Bearer hidden" not in serialized


def test_retrieval_gate_remains_domain_agnostic():
    from ouroboros.agent.working_retrieval import retrieval_requested

    assert retrieval_requested("мы сравнивали две организации вчера: ромашку и петрушку")
    assert retrieval_requested("вернись к прошлому результату")
    assert not retrieval_requested("рассчитай 2 + 2")


def test_detached_retrieval_freezes_late_episodes_and_old_episode_enrichment(tmp_path):
    from ouroboros.agent.context import _build_working_retrieval_section
    from ouroboros.agent.working_retrieval import build_retrieval_section
    from ouroboros.storage.runtime_models import ConversationMessage

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(message_id="anchor", dedup_key="anchor", content="earlier Orion", **TEST_SCOPE.to_dict()),
        scope=TEST_SCOPE,
    )
    old = TaskEpisode(
        **TEST_SCOPE.to_dict(), episode_id="old", task_id="old-task", search_terms=["orion"], answer_preview="before"
    )
    sql_execution_memory_repository(store.engine).record_task_episode(old)
    request = TaskRequest(
        id="frozen", detached=True, through_message_id="anchor", text="earlier Orion", scope=TEST_SCOPE.to_dict()
    )
    job = runtime_persistence.current.create_root_task.execute(request, enabled=True, max_active=5)
    env = SimpleNamespace(drive_root=tmp_path)
    first = _build_working_retrieval_section(
        env, job.input, TEST_SCOPE, execution_memory_repository=sql_execution_memory_repository()
    )
    assert "before" in first
    sql_execution_memory_repository(store.engine).record_task_episode(
        old.model_copy(update={"episode_id": "later", "answer_preview": "LATE EPISODE"})
    )
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            message_id="later-message",
            dedup_key="later-message",
            content="later",
            metadata={"task_id": "old-task"},
            **TEST_SCOPE.to_dict(),
        ),
        scope=TEST_SCOPE,
    )
    assert (
        _build_working_retrieval_section(
            env, job.input, TEST_SCOPE, execution_memory_repository=sql_execution_memory_repository()
        )
        == first
    )
    old.answer_preview = "LATE ENRICHMENT"
    sql_execution_memory_repository(store.engine).record_task_episode(old)
    assert (
        _build_working_retrieval_section(
            env, job.input, TEST_SCOPE, execution_memory_repository=sql_execution_memory_repository()
        )
        == ""
    )
    live = _build_working_retrieval_section(
        env, {"text": "earlier Orion"}, TEST_SCOPE, execution_memory_repository=sql_execution_memory_repository()
    )
    assert "LATE" not in live  # The ordinary hint now excludes this old task.
    live = build_retrieval_section(
        store.tasks_repo,
        "earlier Orion",
        execution_memory_repository=sql_execution_memory_repository(store.engine),
        scope=TEST_SCOPE,
    )
    assert "LATE ENRICHMENT" in live and "LATE EPISODE" in live
    assert (
        build_retrieval_section(
            store.tasks_repo,
            "calculate 2 + 2",
            execution_memory_repository=sql_execution_memory_repository(store.engine),
            scope=TEST_SCOPE,
        )
        == ""
    )


def test_retrieval_respects_token_budget_for_first_card(tmp_path):
    from ouroboros.agent.working_retrieval import build_retrieval_section

    store = runtime_persistence.initialize()
    sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(
            **TEST_SCOPE.to_dict(),
            episode_id="large-card",
            task_id="task-large",
            user_text="Погода в Москве",
            answer_preview="Солнечно " * 100,
            search_terms=["погод", "москв"],
        )
    )

    section = build_retrieval_section(
        store.tasks_repo,
        "Что мы раньше выяснили про погоду в Москве?",
        execution_memory_repository=sql_execution_memory_repository(store.engine),
        scope=TEST_SCOPE,
        token_budget=1,
    )

    assert section == ""


def test_retrieval_skips_legacy_failed_task_episode(tmp_path):
    from ouroboros.agent.working_retrieval import build_retrieval_section

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(
            id="failed-task",
            text="Сравни Ромашку и Петрушку",
            scope=TEST_SCOPE.to_dict(),
        ),
        status="failed",
    )
    sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(
            **TEST_SCOPE.to_dict(),
            episode_id="failed-episode",
            task_id="failed-task",
            user_text="Сравни Ромашку и Петрушку",
            answer_preview="Ошибка.",
            search_terms=["ромашк", "петрушк"],
        )
    )

    section = build_retrieval_section(
        store.tasks_repo,
        "Что мы раньше выяснили про Ромашку и Петрушку?",
        execution_memory_repository=sql_execution_memory_repository(store.engine),
        scope=TEST_SCOPE,
    )

    assert section == ""


def test_retrieval_uses_the_successful_current_attempt_for_a_logical_job(tmp_path):
    from ouroboros.agent.working_retrieval import build_retrieval_section

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="logical-job", detached=True, text="Find Orion", scope=TEST_SCOPE.to_dict()),
    )
    claim = store.tasks_repo.claim_next_task(runtime_id="runtime")
    store.tasks_repo.transition_running(
        "logical-job",
        result="Retry",
        kind="instance_released",
        runtime_id="runtime",
        claim_token=claim.metadata["execution_claim"],
        attempt=1,
        successor=TaskRequest(id="attempt-2", detached=True, text="Find Orion", scope=TEST_SCOPE.to_dict()),
    )
    claim = store.tasks_repo.claim_next_task(runtime_id="runtime")
    store.tasks_repo.finish_detached_attempt(
        "attempt-2", status="completed", result="Orion", claim_token=claim.metadata["execution_claim"]
    )
    sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(
            **TEST_SCOPE.to_dict(),
            episode_id="retry-success",
            task_id="logical-job",
            user_text="Find Orion",
            answer_preview="Orion was found.",
            search_terms=["orion"],
        )
    )
    assert store.tasks_repo.get_current_job("logical-job", scope=TEST_SCOPE).status == "completed"

    section = build_retrieval_section(
        store.tasks_repo,
        "What did we discuss earlier about Orion?",
        execution_memory_repository=sql_execution_memory_repository(store.engine),
        scope=TEST_SCOPE,
    )

    assert "Orion was found" in section


@pytest.mark.parametrize("logical", [False, True])
def test_stored_episode_merges_universal_invocation_index_and_logical_replays_are_immutable(tmp_path, logical):
    from ouroboros.storage.task_episodes import store_task_episode

    repository = sql_execution_memory_repository()
    repository.record_tool_invocation(
        ToolInvocation(
            **TEST_SCOPE.to_dict(),
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
        {"id": "task-indexed", "job_id": "logical" if logical else "", "detached": logical, "text": "Найди Orion"},
        "Нашёл DOC-42",
        {"tool_calls": [{"tool": "custom_extension.lookup"}]},
        scope=TEST_SCOPE,
        search_terms=["orion"],
        created_at="2026-07-01T10:00:00+00:00",
        execution_memory_repository=repository,
    )
    assert episode.tools_used == ["custom_extension.lookup"] and "orion" in episode.search_terms
    assert episode.expires_at == "2026-09-29T10:00:00+00:00"
    assert episode.entities == {"projects": ["Orion"]}
    assert episode.refs["document_id"] == "DOC-42"
    assert episode.refs["tool_invocation_ids"] == ["call-indexed"]
    if logical:
        replay = store_task_episode(
            {"id": "task-indexed", "job_id": "logical", "detached": True, "text": "changed"},
            "changed response",
            {},
            scope=TEST_SCOPE,
            search_terms=["later"],
            created_at="2030-01-01T00:00:00+00:00",
            retention_days=999,
            execution_memory_repository=repository,
        )
        assert replay.model_dump() == episode.model_dump()
        assert len(repository.list_task_episodes(scope=TEST_SCOPE)) == 1


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
    from ouroboros.agent.working_retrieval import build_retrieval_section, episode_search_terms
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.storage.task_episodes import store_task_episode
    from tests.helpers.shared import make_catalog

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
        execution_memory_repository=sql_execution_memory_repository(),
        tool_name=tool_name,
        result='{"employee":"Иванов","grade":"G12"}',
        tool_args={"employee": "Иванов"},
        catalog=make_catalog(
            [
                McpToolProvider(
                    servers=[SimpleNamespace(id="hr-provider")],
                    server_ids_reader=StubMcpServerIdsReader(["hr-provider"]),
                    auth_dependencies=null_egress_auth_dependencies(),
                )
            ]
        ),
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

    task = {
        "id": "task-pulse",
        "text": " ".join(f"question-term-{index}" for index in range(100)),
        "chat_id": 1,
    }
    response = " ".join(f"answer-term-{index}" for index in range(100))
    episode = store_task_episode(
        task,
        response,
        {"tool_calls": []},
        scope=TEST_SCOPE,
        search_terms=episode_search_terms(task, response, {"tool_calls": []}),
        execution_memory_repository=sql_execution_memory_repository(),
    )

    assert episode is not None
    assert len(episode.search_terms) == 120
    assert episode.search_terms[:4] == ["pulse", "пульс", "pulse_people", "heimdall"]
    assert "answer-term-99" not in episode.search_terms

    section = build_retrieval_section(
        store.tasks_repo,
        "Что мы раньше смотрели в Пульсе?",
        execution_memory_repository=sql_execution_memory_repository(store.engine),
        scope=TEST_SCOPE,
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

    repository = sql_execution_memory_repository()
    _record_working_task_episode(
        SimpleNamespace(drive_root=tmp_path),
        {
            "id": "task-11",
            "text": "Найди Orion",
            "chat_id": 1,
        },
        "Нашёл DOC-11",
        {"tool_calls": []},
        TEST_SCOPE,
        execution_memory_repository=repository,
    )

    episodes = repository.list_task_episodes(scope=TEST_SCOPE)
    assert len(episodes) == 1
    assert episodes[0].task_id == "task-11"


def test_supervisor_cleanup_records_checkpoint(tmp_path, monkeypatch):
    from supervisor.runtime import _maybe_cleanup_working_retrieval

    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS", "24")
    store = runtime_persistence.initialize()
    sql_execution_memory_repository(store.engine).record_tool_invocation(
        ToolInvocation(
            **TEST_SCOPE.to_dict(),
            tool_invocation_id="expired-supervisor-call",
            tool_name="custom.lookup",
            created_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-02T00:00:00+00:00",
        )
    )

    _maybe_cleanup_working_retrieval(
        SimpleNamespace(
            working_retrieval_repository=SqlWorkingRetrievalRepository(
                store.engine, scratchpad=SqlScratchpad(store.engine)
            ),
            cleanup_checkpoint_repository=sql_cleanup_checkpoint_repository(store.engine),
        ),
        now_iso="2026-07-01T00:00:00+00:00",
    )

    assert sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-1") == []
    from new_core.identity import ServiceJob
    from ouroboros.contracts.owner_scope import service_identity

    # The sweep's checkpoint is the instance's own row, named by the job that writes it.
    state = (
        sql_owner_state_repository(store.engine)
        .load(service_identity(job=ServiceJob.CONTEXT_CLEANUP).as_scope())
        .payload
    )
    assert state is not None
    assert state["working_retrieval_cleanup"]["removed"]["tool_invocations"] == 1


@pytest.fixture(autouse=True)
def _clear_encryption_cache():
    reset_encryption_cache_for_tests()
    yield
    reset_encryption_cache_for_tests()


@pytest.fixture
def encryption_env(request, monkeypatch) -> bool:
    configured = bool(getattr(request, "param", True))
    if not configured:
        monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", raising=False)
        monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
        reset_encryption_cache_for_tests()
        return configured
    key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", key)
    reset_encryption_cache_for_tests()
    return configured


def _store_on(engine) -> RuntimePersistence:
    """A store over a fresh database, with the schema brought up first.

    The constructor stopped migrating: schema is a deployment step, so a test that wants a
    usable database says so — the same way a deployment does.
    """
    from infra.migrations.runner import upgrade_head

    upgrade_head(engine)
    return make_sql_runtime_persistence(engine, db_url=str(engine.url.render_as_string(hide_password=False)))


@pytest.mark.parametrize("encryption_env", [False, True], indirect=True)
def test_retrieval_round_trip_preserves_payloads_and_encrypts_the_named_fields(blank_schema, encryption_env):
    encrypted = encryption_env
    store = _store_on(blank_schema())
    tool = sql_execution_memory_repository(store.engine).record_tool_invocation(
        ToolInvocation(
            **TEST_SCOPE.to_dict(),
            tool_invocation_id="tool-1",
            task_id="task-1",
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
    episode = sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(
            **TEST_SCOPE.to_dict(),
            episode_id="episode-1",
            task_id="task-1",
            user_text="секретный запрос",
            answer_preview="секретный ответ",
            tools_used=["pulse_people_search"],
            domains=["pulse"],
            entities={"metrics": ["Чистая прибыль / убыток"]},
            refs={"tool_invocation_ids": [tool.tool_invocation_id]},
            search_terms=["pulse", "чистая прибыль", "тб"],
            created_at="2026-06-30T09:01:00+00:00",
        )
    )
    assert (
        sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-1")[0].model_dump()
        == tool.model_dump()
    )
    assert (
        sql_execution_memory_repository(store.engine).list_task_episodes(scope=TEST_SCOPE)[0].model_dump()
        == episode.model_dump()
    )
    with store.engine.connect() as conn:
        for table, columns in (
            ("task_episodes", "user_text, answer_preview, payload_json"),
            ("tool_invocations", "args_redacted_json, result_summary_json, payload_json"),
        ):
            row = conn.execute(text(f"SELECT {columns} FROM {table}")).fetchone()
            assert all(str(value).startswith("enc:v1:") == encrypted for value in row)


def test_retrieval_gate_is_domain_agnostic() -> None:
    from ouroboros.agent.working_retrieval import retrieval_requested

    assert retrieval_requested("мы сравнивали две организации вчера: ромашку и петрушку")
    assert retrieval_requested("вернись к прошлому поручению")
    assert not retrieval_requested("данные каких ТБ у тебя есть?")
    assert not retrieval_requested("создай встречу на сегодня")
    assert not retrieval_requested("посмотри PL по всем банкам")


def test_emit_task_results_skips_working_episode_for_failed_task(monkeypatch, tmp_path) -> None:
    """The failed verdict is also what stops the distillation event; see the adapter tests."""
    from ouroboros.agent import task_pipeline

    recorded: list[str] = []

    monkeypatch.setattr(task_pipeline, "_store_task_result", lambda *args, **kwargs: "failed")
    monkeypatch.setattr(
        task_pipeline, "_record_working_task_episode", lambda *args, **kwargs: recorded.append("episode")
    )
    monkeypatch.setattr(task_pipeline, "record_event", lambda *args, **kwargs: None)

    env = SimpleNamespace(drive_root=tmp_path)
    task_failed = task_pipeline.emit_task_results(
        env,
        llm=object(),
        pending_events=[],
        task={"id": "failed-task", "type": "task", "chat_id": 1, "text": "do it"},
        text="failed",
        usage={},
        llm_trace={"tool_calls": []},
        start_time=time.time(),
        drive_logs=tmp_path / "logs",
        ctx=SimpleNamespace(scope=TEST_SCOPE),
        scope=TEST_SCOPE,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert recorded == []
    assert task_failed is True


def test_cleanup_expired_working_retrieval_rows(blank_schema) -> None:
    store = _store_on(blank_schema())
    sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(
            **TEST_SCOPE.to_dict(),
            episode_id="expired-episode",
            task_id="task-1",
            user_text="old",
            answer_preview="old",
            expires_at="2020-01-01T00:00:00+00:00",
        )
    )
    sql_execution_memory_repository(store.engine).record_tool_invocation(
        ToolInvocation(
            **TEST_SCOPE.to_dict(),
            tool_invocation_id="expired-tool",
            task_id="task-1",
            tool_name="send_assignment",
            status="ok",
            expires_at="2020-01-01T00:00:00+00:00",
        )
    )

    removed = SqlWorkingRetrievalRepository(
        store.engine, scratchpad=SqlScratchpad(store.engine)
    ).cleanup_expired_working_retrieval(now_iso="2026-01-01T00:00:00+00:00", limit=100)

    assert removed == {"task_episodes": 1, "tool_invocations": 1, "scratchpad_blocks": 0}
    assert sql_execution_memory_repository(store.engine).list_task_episodes(scope=TEST_SCOPE) == []
    assert sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-1") == []


# --- the contract this change pins ------------------------------------------


def test_no_task_id_yields_no_episode():
    """An episode has nothing to enrich from and nothing to be filtered by without a task."""
    from ouroboros.storage.task_episodes import build_task_episode

    episode = build_task_episode(
        {"text": "сделай что-нибудь"},
        "готово",
        {"tool_calls": [{"tool": "data_read", "args": {}, "result": "ok"}]},
        scope=TEST_SCOPE,
        search_terms=[],
    )

    assert episode is None


def test_store_task_episode_writes_nothing_without_a_task(tmp_path):
    from ouroboros.storage.task_episodes import store_task_episode

    repository = sql_execution_memory_repository()

    stored = store_task_episode(
        {"text": "сделай"},
        "готово",
        {"tool_calls": []},
        scope=TEST_SCOPE,
        search_terms=[],
        execution_memory_repository=repository,
    )

    assert stored is None
    assert repository.list_task_episodes(scope=TEST_SCOPE) == []


def test_the_invocation_read_has_no_unfiltered_call(tmp_path):
    """There is no argument value — empty, ``None`` or otherwise — that widens this read."""
    store = runtime_persistence.initialize()
    for task_id in ("task-mine", "task-theirs"):
        sql_execution_memory_repository(store.engine).record_tool_invocation(
            ToolInvocation(
                **TEST_SCOPE.to_dict(),
                tool_invocation_id=f"call-{task_id}",
                task_id=task_id,
                tool_name="data_read",
            )
        )

    assert [
        item.task_id
        for item in sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-mine")
    ] == ["task-mine"]
    assert sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="") == []
    with pytest.raises(TypeError):
        sql_execution_memory_repository(store.engine).list_tool_invocations(limit=100)  # type: ignore[call-arg]


def test_enrichment_never_merges_another_tasks_invocations(tmp_path):
    from ouroboros.storage.task_episodes import store_task_episode

    repository = sql_execution_memory_repository()
    repository.record_tool_invocation(
        ToolInvocation(
            **TEST_SCOPE.to_dict(),
            tool_invocation_id="call-mine",
            task_id="task-mine",
            tool_name="custom.lookup",
            result_index={"refs": {"document_id": "DOC-MINE"}, "search_terms": ["mine"]},
            search_terms=["mine"],
        )
    )
    repository.record_tool_invocation(
        ToolInvocation(
            **TEST_SCOPE.to_dict(),
            tool_invocation_id="call-theirs",
            task_id="task-theirs",
            tool_name="custom.lookup",
            result_index={"refs": {"document_id": "DOC-THEIRS"}, "search_terms": ["theirs"]},
            search_terms=["theirs"],
        )
    )

    episode = store_task_episode(
        {"id": "task-mine", "text": "найди"},
        "нашёл",
        {"tool_calls": []},
        scope=TEST_SCOPE,
        search_terms=[],
        execution_memory_repository=repository,
    )

    assert episode is not None
    assert episode.refs["document_id"] == "DOC-MINE"
    assert episode.refs["tool_invocation_ids"] == ["call-mine"]
    assert "theirs" not in episode.search_terms


def test_an_invocation_is_found_through_its_task_whatever_its_axes(tmp_path):
    """Ownership is recorded on the row; the read predicate stays the task alone (D3)."""
    from ouroboros.storage.task_episodes import store_task_episode
    from tests.helpers.shared import scope_with

    repository = sql_execution_memory_repository()
    other_chat = scope_with(TEST_SCOPE, chat_id="999")
    repository.record_tool_invocation(
        ToolInvocation(
            **other_chat.to_dict(),
            tool_invocation_id="call-elsewhere",
            task_id="task-shared",
            tool_name="custom.lookup",
            result_index={"refs": {"document_id": "DOC-7"}},
        )
    )

    episode = store_task_episode(
        {"id": "task-shared", "text": "найди"},
        "нашёл",
        {"tool_calls": []},
        scope=TEST_SCOPE,
        search_terms=[],
        execution_memory_repository=repository,
    )

    assert episode is not None
    assert episode.refs["document_id"] == "DOC-7"


@pytest.mark.parametrize("axis", ["chat_id", "surface_id"])
def test_an_episode_of_another_scope_is_not_a_candidate(tmp_path, axis):
    """Identical search terms, so only the predicate can separate the two episodes."""
    from ouroboros.agent.working_retrieval import build_retrieval_section
    from tests.helpers.shared import scope_with

    other = scope_with(TEST_SCOPE, **{axis: "dialogs" if axis == "surface_id" else "999"})
    store = runtime_persistence.initialize()
    for name, scope in (("mine", TEST_SCOPE), ("theirs", other)):
        sql_execution_memory_repository(store.engine).record_task_episode(
            TaskEpisode(
                **scope.to_dict(),
                episode_id=f"episode-{name}",
                task_id=f"task-{name}",
                created_at="2026-07-01T10:00:00+00:00",
                user_text=f"вопрос {name}",
                answer_preview=f"ответ {name}",
                search_terms=["погода", "москва"],
            )
        )

    section = build_retrieval_section(
        store.tasks_repo,
        "Что мы раньше выяснили про погоду в Москве?",
        execution_memory_repository=sql_execution_memory_repository(store.engine),
        scope=TEST_SCOPE,
    )

    assert "task-mine" in section
    assert "task-theirs" not in section


def test_a_malformed_scope_surfaces_instead_of_emptying_the_section(tmp_path, monkeypatch):
    """A programming error must not present as "retrieval quietly stopped firing" (D5)."""
    from ouroboros.agent import context as context_module

    monkeypatch.setattr(
        context_module,
        "_recent_chat_task_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("scope is not a scope")),
    )
    env = SimpleNamespace(drive_root=tmp_path)

    with pytest.raises(TypeError, match="scope is not a scope"):
        context_module._build_working_retrieval_section(
            env,
            {"text": "что мы раньше обсуждали?"},
            TEST_SCOPE,
            execution_memory_repository=sql_execution_memory_repository(),
        )


def test_a_store_failure_is_reported_but_does_not_fail_the_task(tmp_path, caplog, monkeypatch):
    """The swallow that remains is for a store that is down, and it is above debug."""
    from sqlalchemy.exc import OperationalError

    from ouroboros.agent import task_pipeline

    repository = sql_execution_memory_repository()
    with fail_sql("task_episodes", OperationalError("SELECT 1", {}, Exception("store is down"))):
        env = SimpleNamespace(drive_root=tmp_path)
        with caplog.at_level("WARNING", logger="ouroboros.agent.task_pipeline"):
            task_pipeline._record_working_task_episode(
                env,
                {"id": "task-1", "text": "сделай"},
                "готово",
                {"tool_calls": []},
                TEST_SCOPE,
                execution_memory_repository=repository,
            )

        assert "Failed to record working-memory task episode" in caplog.text
