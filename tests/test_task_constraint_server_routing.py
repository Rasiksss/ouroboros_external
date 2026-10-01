from types import SimpleNamespace

from supervisor.orchestrator.intent import SubmissionMode
from supervisor.runtime import SupervisorLoop


class FakeBridge:
    def get_updates(self, offset, timeout=1):
        return [
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 1},
                    "from": {"id": 1},
                    "text": "repair skill",
                    "task_constraint": {
                        "mode": "skill_repair",
                        "skill_name": "alpha",
                        "payload_root": "skills/external/alpha",
                    },
                    "suppress_chat_log": True,
                },
            }
        ]


def test_constrained_repair_is_not_injected_into_busy_agent(monkeypatch, tmp_path) -> None:
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    calls = {"inject": 0, "queued": []}
    agent = SimpleNamespace(
        _busy=True,
        inject_message=lambda *a, **k: calls.__setitem__("inject", calls["inject"] + 1),
    )
    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    repo = get_state_repository(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    orchestrator = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=repo,
        agent_getter=lambda: agent,
    )

    ctx = SimpleNamespace(
        state_repo=repo,
        orchestrator=orchestrator,
        send_with_budget=lambda *a, **k: None,
        save_state=lambda st: None,
        load_state=lambda: OwnerRuntimeState(owner_id=1),
        executor=SimpleNamespace(ensure_healthy=lambda: None),
        task_queue=task_queue,
    )
    loop = SupervisorLoop(ctx=ctx, bridge=FakeBridge(), stop_event=SimpleNamespace(is_set=lambda: False))

    original_submit = orchestrator.submit

    def _track_submit(envelope):
        result = original_submit(envelope)
        if result.mode == SubmissionMode.QUEUE:
            calls["queued"].append(dict(envelope.task_constraint))
        return result

    orchestrator.submit = _track_submit  # type: ignore[method-assign]
    loop.process_bridge_updates(0)

    assert calls["inject"] == 0
    assert calls["queued"] == [
        {
            "mode": "skill_repair",
            "skill_name": "alpha",
            "payload_root": "skills/external/alpha",
        }
    ]
