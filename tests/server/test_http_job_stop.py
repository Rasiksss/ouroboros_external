from new_core.coordination.interfaces import TaskStopNotice
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE


@parametrize_from_dict({"disabled": {"enabled": False}, "enabled": {"enabled": True}})
def test_single_process_http_stop_uses_the_runtime_consumer(monkeypatch, tmp_path, enabled):
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(enabled).lower())
    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True) as client:
        import server

        store = runtime_persistence.current
        store.tasks_repo.create_task(TaskRequest(id="http-stop", text="work", scope=TEST_SCOPE.to_dict()))
        assert client.get("/api/state").json()["pending_count"] == 1
        store.tasks_repo.claim_next_task(runtime_id="runtime-a")
        assert client.get("/api/state").json()["running_count"] == 1

        assert client.get("/api/jobs").json()["detached_enabled"] is enabled
        response = client.post("/api/jobs/http-stop/stop")

        assert response.status_code == 200
        assert server._task_stop_consumer.poll(0.0) == TaskStopNotice(runtime_id="runtime-a", task_ids=("http-stop",))
