from __future__ import annotations

import time

from tests.helpers.database import refused_runtime_database
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.server_client import patched_server_client


def _patched_test_client(monkeypatch, tmp_path):
    return patched_server_client(monkeypatch, tmp_path)


@parametrize_from_dict(
    {
        "startup": {"path": "/start"},
        "readiness": {"path": "/ready"},
        "liveness": {"path": "/live"},
    }
)
def test_health_probe_endpoints_return_200(monkeypatch, tmp_path, path: str):
    with _patched_test_client(monkeypatch, tmp_path) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.text == ""


def test_readiness_and_startup_wait_for_the_supervisor(monkeypatch, tmp_path) -> None:
    with _patched_test_client(monkeypatch, tmp_path) as client:
        import server as srv

        srv._supervisor_ready.clear()
        readiness = client.get("/ready")

        assert readiness.status_code == 503
        # "starting" names no tier: the probe answers about the process answering it.
        assert readiness.text == "starting"
        assert client.get("/start").status_code == 503
        assert client.get("/live").status_code == 200, "the startup probe owns this window"


def test_readiness_fails_on_an_unreachable_database(monkeypatch, tmp_path) -> None:
    with _patched_test_client(monkeypatch, tmp_path) as client, refused_runtime_database():
        assert client.get("/ready").status_code == 503


def test_a_dead_loop_fails_readiness_and_liveness_once_the_start_is_over(monkeypatch, tmp_path) -> None:
    with _patched_test_client(monkeypatch, tmp_path) as client:
        import server as srv

        srv._supervisor_error = "Supervisor loop stopped: aborted after 3 consecutive tick failures"
        for path in ("/ready", "/live"):
            response = client.get(path)
            assert response.status_code == 503
            assert "aborted" in response.text
        assert client.get("/start").status_code == 200, "liveness orders the restart, sooner than the startup budget"


def test_liveness_fails_when_the_agent_runtime_gave_up(monkeypatch, tmp_path) -> None:
    with _patched_test_client(monkeypatch, tmp_path) as client:
        import server as srv

        srv._agent_runtime_thread._error = "Agent runtime crash #3: boom"
        response = client.get("/live")

    assert response.status_code == 503
    assert "Agent runtime crash #3" in response.text


class _CrashingRuntime:
    def __init__(self, crashes: int) -> None:
        self.crashes = crashes

    def execute(self) -> None:
        if self.crashes:
            self.crashes -= 1
            raise RuntimeError("boom")


def test_the_agent_runtime_records_an_error_only_when_it_gives_up(monkeypatch, tmp_path) -> None:
    with _patched_test_client(monkeypatch, tmp_path):
        import server as srv

        monkeypatch.setattr(srv._stop_requested, "wait", lambda timeout: False)

        recovered = srv._AgentRuntimeThread()
        recovered._runtime = _CrashingRuntime(crashes=1)
        recovered._run()
        assert recovered.error is None, "a crash the loop came back from is not a failure liveness reports"

        given_up = srv._AgentRuntimeThread()
        given_up._runtime = _CrashingRuntime(crashes=srv._AgentRuntimeThread.MAX_RESTARTS)
        given_up._run()
        assert given_up.error is not None
        assert f"#{srv._AgentRuntimeThread.MAX_RESTARTS}" in given_up.error


@parametrize_from_dict(
    {
        "supervisor": {"attribute": "_supervisor_heartbeat", "loop": "supervisor"},
        "agent runtime": {"attribute": "_agent_heartbeat", "loop": "agent runtime"},
    }
)
def test_liveness_fails_when_a_loop_stops_ticking(monkeypatch, tmp_path, attribute: str, loop: str) -> None:
    from ouroboros.observability.health import LoopHeartbeat

    with _patched_test_client(monkeypatch, tmp_path) as client:
        import server as srv

        stale = LoopHeartbeat(loop, stall_after_sec=0.0)
        stale.beat()
        monkeypatch.setattr(srv, attribute, stale)
        time.sleep(0.01)
        response = client.get("/live")
        assert client.get("/ready").status_code == 200, "a stall is liveness' to report"

    assert response.status_code == 503
    assert f"{loop} loop" in response.text


def test_a_silent_memory_loop_counts_only_while_its_task_is_alive(monkeypatch, tmp_path) -> None:
    from ouroboros.observability.health import LoopHeartbeat

    with _patched_test_client(monkeypatch, tmp_path) as client:
        import server as srv

        stale = LoopHeartbeat("memory runtime", stall_after_sec=0.0)
        stale.beat()
        monkeypatch.setattr(srv, "_memory_heartbeat", stale)
        time.sleep(0.01)
        assert client.get("/live").status_code == 200, "a memory loop that gave up is not on liveness"

        monkeypatch.setattr(srv._memory_runtime, "is_alive", lambda: True)
        response = client.get("/live")

    assert response.status_code == 503
    assert "memory runtime loop" in response.text
