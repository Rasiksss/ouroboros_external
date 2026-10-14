from infra.runtime_database import create_runtime_engine
from ouroboros.storage.runtime_models import ConversationMessage, TaskRequest
from tests.helpers.conversations import make_sql_runtime_persistence, sql_conversation_repository
from tests.helpers.files import stored_file
from tests.helpers.parametrize import parametrize_from_dict


@parametrize_from_dict(
    {
        "text_only": {"with_attachment": False},
        "linked_file": {"with_attachment": True},
    }
)
def test_root_admission_captures_history_with_one_connection(postgres_task_scope, monkeypatch, with_attachment):
    urls, scope = postgres_task_scope
    monkeypatch.setenv("OUROBOROS_DB_POOL_TIMEOUT_SEC", "1")
    files = [stored_file(scope=scope)] if with_attachment else []
    engine = create_runtime_engine(urls, pool_size=1, max_overflow=0)
    store = make_sql_runtime_persistence(engine, db_url=urls[0], db_urls=urls)
    try:
        message = sql_conversation_repository(engine).append_message(
            ConversationMessage(
                **scope.to_dict(),
                message_id="submission",
                dedup_key="submission",
                actor_type="owner",
                source="browser",
                content="Read my message",
                metadata={"attachments": files},
            ),
            scope=scope,
        )
        admitted = store.create_root_task.execute(
            TaskRequest(id="admitted", scope=scope.to_dict(), through_message_id=message.message_id),
            enabled=True,
            max_active=1,
        )
        assert admitted.status == "queued"
        assert admitted.input["context_captured"] is True
        assert admitted.input["history_message_ids"] == [message.message_id]
        history = sql_conversation_repository(engine).list_chat_history_messages(scope=scope)
        assert [item.message_id for item in history] == [message.message_id]
        assert [item["id"] for item in history[0].attachments] == [item["id"] for item in files]
        if files:
            assert history[0].attachments[0]["filename"] == files[0]["filename"]
    finally:
        engine.dispose()
