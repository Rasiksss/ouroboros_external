from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
import redis
from starlette import status as http_status
from starlette.applications import Starlette
from starlette.testclient import TestClient

from new_core.coordination.inprocess import NullWorkSignal
from new_core.coordination.interfaces import ITaskStopPublisher, TaskStopNotice
from ouroboros.app.task_stop_notifications import TaskStopNotifier
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.queue import TaskQueue
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity
from transport.browser.router import collect_routes


@pytest.fixture(params=[False, True])
def stop_client(tmp_path, request, monkeypatch):
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(request.param).lower())
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    store = runtime_persistence.current
    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
    app = Starlette(routes=collect_routes(data_dir=tmp_path, repo_dir=tmp_path))
    app.state.detached_enabled = request.param
    publisher = Mock(spec=ITaskStopPublisher)
    app.state.task_stop_notifier = TaskStopNotifier(publisher)
    app.state.message_gateway = runtime.gateway
    try:
        with TestClient(app) as client:
            client.headers.update({"X-Ouroboros-Owner-Id": TEST_SCOPE.owner_id, "X-Ouroboros-Chat-Id": "1"})
            yield client, store, publisher
    finally:
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


@parametrize_from_dict({"queued": {"running": False}, "running": {"running": True}})
def test_http_foreground_is_visible_and_stoppable_before_agent_output(stop_client, tmp_path, running):
    client, store, _publisher = stop_client
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    foreground = queue.enqueue(TaskRequest(text="foreground work", scope=TEST_SCOPE.to_dict()))
    if running:
        assert store.tasks_repo.claim_next_task(runtime_id="runtime-a").task_id == foreground.task_id
    background = queue.enqueue(TaskRequest(text="background work", detached=True, scope=TEST_SCOPE.to_dict()))

    response = client.get("/api/jobs")

    assert response.status_code == http_status.HTTP_200_OK
    payload = response.json()
    assert payload["truncated"] is False
    assert {job["job_id"] for job in payload["jobs"]} == {foreground.job_id, background.job_id}
    listed = next(job for job in payload["jobs"] if not job["detached"])
    assert listed == {
        "job_id": foreground.job_id,
        "detached": False,
        "surface_id": "browser",
        "chat_id": TEST_SCOPE.chat_id,
        "status": "running" if running else "queued",
        "created_at": foreground.created_at,
        "started_at": store.tasks_repo.get_task(foreground.task_id).started_at or None,
        "preview": "foreground work",
    }
    assert sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent") == []

    stopped = client.post(f"/api/jobs/{listed['job_id']}/stop", params={"notify_chat": "false"})

    assert stopped.status_code == http_status.HTTP_200_OK
    assert stopped.json()["job"]["status"] == ("stopping" if running else "cancelled")
    assert store.tasks_repo.get_task(background.task_id) == background


def test_http_stop_publishes_committed_tree_notices_off_the_event_loop(stop_client):
    client, store, publisher = stop_client
    for task_id, parent, status, runtime in (
        ("root", "", "running", "runtime-a"),
        ("child-a", "root", "running", "runtime-a"),
        ("child-b", "root", "running", "runtime-b"),
        ("waiting", "root", "queued", None),
        ("other", "", "queued", None),
    ):
        store.tasks_repo.create_task(
            TaskRequest(id=task_id, parent_task_id=parent, text=task_id, scope=TEST_SCOPE.to_dict())
        )
        if status == "running":
            assert store.tasks_repo.claim_next_task(runtime_id=runtime).task_id == task_id

    observations = []

    def observe(notice):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_event_loop = False
        else:
            on_event_loop = True
        observations.append(
            (on_event_loop, all(store.tasks_repo.get_task(task_id).stop_requested_at for task_id in notice.task_ids))
        )

    publisher.publish.side_effect = observe
    response = client.post("/api/jobs/root/stop")

    assert response.status_code == 200
    assert response.json()["job"]["status"] == "stopping"
    assert observations == [(False, True), (False, True)]
    assert [call.args[0] for call in publisher.publish.call_args_list] == [
        TaskStopNotice(runtime_id="runtime-a", task_ids=("root", "child-a")),
        TaskStopNotice(runtime_id="runtime-b", task_ids=("child-b",)),
    ]
    assert store.tasks_repo.get_task("waiting").status == "cancelled"
    assert store.tasks_repo.get_task("other").status == "queued"


def test_http_stop_keeps_committed_intent_and_other_notices_after_publisher_failure(stop_client, caplog):
    client, store, publisher = stop_client
    for task_id, parent, runtime in (("root", "", "runtime-a"), ("child", "root", "runtime-b")):
        store.tasks_repo.create_task(
            TaskRequest(id=task_id, parent_task_id=parent, text=task_id, scope=TEST_SCOPE.to_dict())
        )
        assert store.tasks_repo.claim_next_task(runtime_id=runtime).task_id == task_id
    publisher.publish.side_effect = [redis.ConnectionError("broker unavailable"), None]

    response = client.post("/api/jobs/root/stop")

    assert response.status_code == 200
    assert publisher.publish.call_count == 2
    assert store.tasks_repo.get_task("root").stop_requested_at
    assert store.tasks_repo.get_task("child").stop_requested_at
    assert "Failed to publish task-stop hint" in caplog.text


@parametrize_from_dict(
    {
        "missing job": {"job_id": "missing", "owner": TEST_SCOPE.owner_id},
        "foreign owner": {"job_id": "root", "owner": "another-owner"},
    }
)
@parametrize_from_dict({"default receipt": {"query": ""}, "silent": {"query": "?notify_chat=false"}})
def test_http_stop_does_not_notify_for_unowned_or_missing_jobs(stop_client, job_id, owner, query):
    client, store, publisher = stop_client
    store.tasks_repo.create_task(TaskRequest(id="root", text="work", scope=TEST_SCOPE.to_dict()), status="running")

    response = client.post(f"/api/jobs/{job_id}/stop{query}", headers={"X-Ouroboros-Owner-Id": owner})

    assert response.status_code == 404
    assert not store.tasks_repo.get_task("root").stop_requested_at
    publisher.publish.assert_not_called()


def test_http_stop_notifies_again_without_moving_the_first_stop_time(stop_client):
    client, store, publisher = stop_client
    store.tasks_repo.create_task(TaskRequest(id="root", text="work", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.claim_next_task(runtime_id="runtime-a")

    assert client.post("/api/jobs/root/stop").status_code == 200
    first = store.tasks_repo.get_task("root").stop_requested_at
    assert client.post("/api/jobs/root/stop").status_code == 200

    assert first and store.tasks_repo.get_task("root").stop_requested_at == first
    assert publisher.publish.call_count == 2


def test_job_views_include_chat_identity_and_app_flag_without_widening_authorization(stop_client, monkeypatch):
    client, store, _publisher = stop_client

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(not client.app.state.detached_enabled).lower())
    remote = scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="remote-chat")
    for index in range(101):
        store.tasks_repo.create_task(TaskRequest(id=f"ordinary-{index:03d}", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.create_task(TaskRequest(id="remote", detached=True, scope=remote.to_dict()))
    for detached in (False, True):
        store.tasks_repo.create_task(
            TaskRequest(id=f"child-{detached}", parent_task_id="remote", detached=detached, scope=remote.to_dict())
        )
        store.tasks_repo.create_task(
            TaskRequest(id=f"finished-{detached}", detached=detached, scope=remote.to_dict()), status="completed"
        )
    for axis in ("owner_id", "agent_id"):
        foreign = scope_with(remote, **{axis: "foreign"})
        for detached in (False, True):
            job_id = f"{axis}-{detached}"
            store.tasks_repo.create_task(TaskRequest(id=job_id, detached=detached, scope=foreign.to_dict()))
            assert client.get(f"/api/jobs/{job_id}").status_code == http_status.HTTP_404_NOT_FOUND
            assert client.post(f"/api/jobs/{job_id}/stop").status_code == http_status.HTTP_404_NOT_FOUND
            assert store.tasks_repo.get_task(job_id).status == "queued"
    assert client.get("/api/jobs", headers={"X-Ouroboros-Owner-Id": ""}).status_code == http_status.HTTP_403_FORBIDDEN
    response = client.get("/api/jobs").json()
    assert response["detached_enabled"] is client.app.state.detached_enabled
    assert response["truncated"] is True
    assert [job["job_id"] for job in response["jobs"]] == ["remote"] + [
        f"ordinary-{index:03d}" for index in range(100, 1, -1)
    ]
    expected = {"surface_id": "dialogs", "chat_id": "remote-chat", "detached": True}
    assert {key: response["jobs"][0][key] for key in expected} == expected
    detail = client.get("/api/jobs/remote").json()["job"]
    assert {key: detail[key] for key in expected} == expected
    assert client.post("/api/jobs/remote/stop").json()["job"]["status"] == "cancelled"


@parametrize_from_dict(
    {
        "omitted": {"query": "", "receipt": True},
        "true": {"query": "?notify_chat=true", "receipt": True},
        "false": {"query": "?notify_chat=false", "receipt": False},
        "mixed case": {"query": "?notify_chat=FaLsE", "receipt": False},
    }
)
def test_http_stop_always_notifies_executors_but_can_suppress_the_chat_receipt(stop_client, query, receipt):
    client, store, publisher = stop_client
    store.tasks_repo.create_task(TaskRequest(id="root", text="work", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.claim_next_task(runtime_id="runtime-a")

    response = client.post(f"/api/jobs/root/stop{query}")

    assert response.status_code == 200
    assert response.json()["job"]["status"] == "stopping"
    assert store.tasks_repo.get_task("root").stop_requested_at
    publisher.publish.assert_called_once_with(TaskStopNotice(runtime_id="runtime-a", task_ids=("root",)))
    inbox = client.app.state.message_gateway.inbox
    if receipt:
        envelope = inbox.poll(0.0)
        assert envelope is not None
        assert envelope.text == "/stop root"
        assert envelope.suppress_chat_log is True
        assert envelope.suppress_acceptance_notice is True
        assert envelope.scope == TEST_SCOPE
    assert inbox.poll(0.0) is None


@parametrize_from_dict({"invalid value": {"value": "invalid"}, "empty value": {"value": ""}})
def test_http_stop_rejects_invalid_notify_chat_before_side_effects(stop_client, value):
    client, store, publisher = stop_client
    store.tasks_repo.create_task(TaskRequest(id="root", text="work", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.claim_next_task(runtime_id="runtime-a")
    store.tasks_repo.create_task(
        TaskRequest(id="child", parent_task_id="root", text="waiting", scope=TEST_SCOPE.to_dict())
    )

    response = client.post("/api/jobs/root/stop", params={"notify_chat": value})

    assert response.status_code == 400
    assert response.json() == {"error": "notify_chat must be true or false"}
    assert not store.tasks_repo.get_task("root").stop_requested_at
    assert store.tasks_repo.get_task("child").status == "queued"
    publisher.publish.assert_not_called()
    assert client.app.state.message_gateway.inbox.poll(0.0) is None
