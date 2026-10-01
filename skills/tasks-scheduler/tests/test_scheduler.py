from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import json
import logging
import pathlib
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError

from ouroboros.contracts.skill_manifest import parse_skill_manifest_text

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE = "test_tasks_scheduler_skill_payload"


@pytest.fixture
def skill_modules():
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        _PACKAGE,
        SKILL_DIR / "plugin.py",
        submodule_search_locations=[str(SKILL_DIR)],
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = plugin
    spec.loader.exec_module(plugin)
    core = importlib.import_module(f"{_PACKAGE}.scheduler_core")
    storage = importlib.import_module(f"{_PACKAGE}.scheduler_storage")
    service = importlib.import_module(f"{_PACKAGE}.scheduler_service")
    yield SimpleNamespace(plugin=plugin, core=core, storage=storage, service=service)
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
            sys.modules.pop(name, None)


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Moscow")).timestamp()


def _schedule(schedule_id: str) -> dict:
    return {
        "id": schedule_id,
        "revision": 1,
        "state": "pending",
        "what": "Prepare report alpha-42",
        "execution_kind": "agent_task",
        "recurrence": "daily",
        "time": "09:00",
        "timezone": "Europe/Moscow",
        "created_at": "2026-07-10T05:50:00+00:00",
        "updated_at": "2026-07-10T05:50:00+00:00",
        "activated_at": None,
        "next_run_at": None,
        "delivery": {"chat_id": 1, "source": "browser"},
    }


def _state_store(tmp_path):
    from ouroboros.skills.extensions.loader import PluginAPIImpl
    from ouroboros.storage.blob.file_backend import FileStorageProvider

    return PluginAPIImpl(
        skill_name="tasks-scheduler",
        permissions=("fs",),
        env_allowlist=(),
        state_dir=tmp_path / "state" / "skills" / "tasks-scheduler",
        settings_reader=dict,
        storage_provider=FileStorageProvider(tmp_path),
    ).get_state_store()


@pytest.fixture
def schedule_store_factory(tmp_path, skill_modules):
    def create_store():
        return skill_modules.storage.SharedScheduleStore(_state_store(tmp_path))

    return create_store


@pytest.fixture
def schedule_store(schedule_store_factory):
    return schedule_store_factory()


def test_manifest_and_payload_do_not_request_or_import_sql() -> None:
    manifest = parse_skill_manifest_text((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))

    assert manifest.name == "tasks-scheduler"
    assert manifest.validate() == []
    assert {"tool", "fs", "read_settings", "supervised_task", "inject_chat", "send_message"} <= set(
        manifest.permissions
    )
    assert manifest.version == "1.1.0"
    payload = "\n".join(path.read_text(encoding="utf-8") for path in SKILL_DIR.glob("*.py"))
    for forbidden in ("sqlalchemy", "runtime_store", "TaskRequest", "_engine", "CREATE TABLE"):
        assert forbidden not in payload


def test_skill_instructions_fail_closed_when_scheduler_tools_are_unavailable() -> None:
    manifest = parse_skill_manifest_text((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    body = manifest.body

    assert "Never use schedule_task" in manifest.description
    assert "extension is not live" in manifest.description
    assert "Never substitute `schedule_task`" in body
    assert "do not claim that a schedule was created" in body
    assert "successful `confirm_schedule`" in body


def test_activation_uses_confirmation_as_lower_bound(skill_modules) -> None:
    core = skill_modules.core
    draft = _schedule("c2c97783-4d2b-4ea3-b3dd-e94df990a024")

    active = core.activate_schedule(draft, now_ts=_ts(2026, 7, 10, 9, 10))

    assert active["state"] == "active"
    assert active["activated_at"] == "2026-07-10T06:10:00+00:00"
    assert active["next_run_at"] == "2026-07-11T06:00:00+00:00"


def test_once_in_the_past_cannot_be_activated(skill_modules) -> None:
    core = skill_modules.core
    draft = _schedule("fb592dd5-ed80-4740-be41-553eed0429d1")
    draft.update({"recurrence": "once", "at": "2026-07-10T08:00:00", "time": None})

    with pytest.raises(core.ScheduleValidationError, match="future"):
        core.activate_schedule(draft, now_ts=_ts(2026, 7, 10, 9, 10))


def test_relative_delay_fields_are_mutually_exclusive(skill_modules, schedule_store) -> None:
    scheduler = skill_modules.service.SchedulerService(
        schedule_store,
        _FakeApi(),
        timezone_name="Europe/Moscow",
        now_fn=lambda: _ts(2026, 7, 10, 8, 0),
    )

    with pytest.raises(skill_modules.core.ScheduleValidationError, match="only one"):
        scheduler.create_scheduled_task(
            SimpleNamespace(current_chat_id=1, current_task_source="browser"),
            instruction="Prepare report alpha-42",
            recurrence="once",
            in_minutes=10,
            in_hours=1,
        )


def test_resume_skips_missed_slots(skill_modules) -> None:
    core = skill_modules.core
    paused = _schedule("0dc9a019-4d69-4742-bc8f-ae159869228c")
    paused.update({"state": "paused", "activated_at": "2026-07-01T06:00:00+00:00"})

    resumed = core.resume_schedule(paused, now_ts=_ts(2026, 7, 10, 12, 0))

    assert resumed["state"] == "active"
    assert resumed["next_run_at"] == "2026-07-11T06:00:00+00:00"


def test_shared_store_uses_cas_and_claims_each_slot_once(skill_modules, schedule_store) -> None:
    storage = skill_modules.storage
    store = schedule_store
    schedule_id = "74e5ec6c-c98e-42e8-a22c-6e4ba7f6c1fa"
    store.create_schedule(_schedule(schedule_id))
    stale = store.get_schedule(schedule_id)
    assert stale is not None
    changed = dict(stale.value)
    changed["what"] = "Changed"
    updated = store.replace_schedule(changed, expected_token=stale.token)

    assert updated.value["revision"] == 2
    with pytest.raises(storage.ConcurrentUpdateError):
        store.replace_schedule(changed, expected_token=stale.token)
    assert store.claim_slot(schedule_id, "1783663200", {"status": "claimed"}) is True
    assert store.claim_slot(schedule_id, "1783663200", {"status": "claimed"}) is False


def test_shared_store_quarantines_corrupt_schedule(skill_modules, schedule_store) -> None:
    storage = skill_modules.storage
    store = schedule_store
    state_store = store.state_store
    schedule_id = "97536be9-e747-4af7-aab2-17f9fb6b7219"
    state_store.put_bytes(f"schedules/{schedule_id}.json", b"{")

    with pytest.raises(storage.StorageCorruptionError):
        store.get_schedule(schedule_id)

    assert state_store.get_bytes(f"schedules/{schedule_id}.json") is None
    assert len(state_store.list_keys("quarantine/")) == 1


def test_shared_store_failure_logs_safe_error_details(skill_modules, caplog) -> None:
    class FailingStateStore:
        def list_keys(self, _prefix):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "access_hash=super-secret"}},
                "ListState",
            )

    storage = skill_modules.storage
    store = storage.SharedScheduleStore(FailingStateStore())
    caplog.set_level(logging.WARNING, logger=storage.__name__)

    with pytest.raises(storage.StorageError, match="failed to list schedules"):
        store.list_schedules()

    operational = "\n".join(record.getMessage() for record in caplog.records)
    assert operational.count("event=tasks_scheduler_storage_error") == 1
    assert "operation=list_schedules" in operational
    assert "error_code=AccessDenied" in operational
    assert "exception_type=ClientError" in operational
    assert "namespace=skill_state" in operational
    assert "access_hash" not in operational
    assert "super-secret" not in operational


class _FakeApi:
    def __init__(self) -> None:
        self.injected: list[dict] = []
        self.sent: list[dict] = []

    def inject_chat(self, text: str, **kwargs) -> None:
        self.injected.append({"text": text, **kwargs})

    def send_message(self, text: str, **kwargs) -> None:
        self.sent.append({"text": text, **kwargs})

    def log(self, *_args, **_kwargs) -> None:
        return None


def test_schedule_can_move_to_current_chat(skill_modules, schedule_store) -> None:
    service_mod = skill_modules.service
    now = _ts(2026, 7, 10, 8, 0)
    scheduler = service_mod.SchedulerService(
        schedule_store,
        _FakeApi(),
        timezone_name="Europe/Moscow",
        now_fn=lambda: now,
    )
    created = scheduler.create_scheduled_task(
        SimpleNamespace(current_chat_id=11, current_task_source="browser"),
        instruction="Prepare report alpha-42",
        recurrence="daily",
        time="09:00",
    )
    assert created["preview_next_run"] == "2026-07-10 09:00 (Europe/Moscow)"

    moved = scheduler.move_schedule_here(
        SimpleNamespace(current_chat_id=77, current_task_source="dialogs"),
        schedule_id=created["id"],
        revision=created["revision"],
    )

    assert moved["delivery"] == {"chat_id": 77, "source": "dialogs"}
    assert moved["revision"] == 2


def test_due_agent_task_is_injected_to_bound_chat_and_completed(skill_modules, schedule_store) -> None:
    service_mod = skill_modules.service
    clock = {"now": _ts(2026, 7, 10, 8, 0)}
    api = _FakeApi()
    store = schedule_store
    scheduler = service_mod.SchedulerService(
        store,
        api,
        timezone_name="Europe/Moscow",
        now_fn=lambda: clock["now"],
    )
    ctx = SimpleNamespace(current_chat_id=42, current_task_source="browser")
    created = scheduler.create_scheduled_task(
        ctx,
        instruction="Prepare report alpha-42",
        recurrence="once",
        in_minutes=10,
    )
    active = scheduler.confirm_schedule(created["id"], revision=created["revision"])
    clock["now"] += 11 * 60

    scheduler.tick()

    stored = store.get_schedule(active["id"])
    assert stored is not None
    assert stored.value["state"] == "completed"
    assert api.injected[0]["chat_id"] == 42
    assert api.injected[0]["delivery_source"] == "browser"
    assert "Prepare report alpha-42" in api.injected[0]["text"]
    assert api.sent == []


def test_stale_claim_is_not_retried_and_requires_attention(skill_modules, schedule_store) -> None:
    service_mod = skill_modules.service
    clock = {"now": _ts(2026, 7, 10, 8, 0)}
    api = _FakeApi()
    store = schedule_store
    scheduler = service_mod.SchedulerService(
        store,
        api,
        timezone_name="Europe/Moscow",
        now_fn=lambda: clock["now"],
    )
    created = scheduler.create_scheduled_task(
        SimpleNamespace(current_chat_id=42, current_task_source="browser"),
        instruction="Prepare report alpha-42",
        recurrence="once",
        in_minutes=10,
    )
    active = scheduler.confirm_schedule(created["id"], revision=created["revision"])
    clock["now"] += 20 * 60
    slot_ts = str(int(skill_modules.core.parse_iso_timestamp(active["next_run_at"])))
    assert store.claim_slot(
        active["id"],
        slot_ts,
        {
            "schedule_id": active["id"],
            "slot_id": slot_ts,
            "status": "claimed",
            "claimed_at": skill_modules.core.utc_iso(clock["now"] - 600),
        },
    )

    scheduler.tick()

    stored = store.get_schedule(active["id"])
    assert stored is not None
    assert stored.value["state"] == "needs_attention"
    assert api.injected == []


def test_scheduled_task_cannot_rebind_delivery(skill_modules, schedule_store) -> None:
    service_mod = skill_modules.service
    scheduler = service_mod.SchedulerService(
        schedule_store,
        _FakeApi(),
        timezone_name="Europe/Moscow",
        now_fn=lambda: _ts(2026, 7, 10, 8, 0),
    )
    created = scheduler.create_scheduled_task(
        SimpleNamespace(current_chat_id=11, current_task_source="browser"),
        instruction="Prepare report alpha-42",
        recurrence="daily",
        time="09:00",
    )

    with pytest.raises(service_mod.SchedulerError, match="interactive"):
        scheduler.move_schedule_here(
            SimpleNamespace(current_chat_id=11, current_task_source="internal"),
            schedule_id=created["id"],
            revision=created["revision"],
        )


def test_extension_loads_through_project_loader(tmp_path) -> None:
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.skills.loader import find_skill
    from ouroboros.storage.blob.file_backend import FileStorageProvider

    extension_loader.unload_extension("tasks-scheduler")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    provider = FileStorageProvider(drive_root)
    loaded = find_skill(drive_root, "tasks-scheduler", repo_path=str(SKILL_DIR.parent))
    assert loaded is not None

    try:
        error = extension_loader.load_extension(
            loaded,
            lambda: {
                "OUROBOROS_ENV": "development",
                "OUROBOROS_TIMEZONE": "Europe/Moscow",
            },
            drive_root=drive_root,
            storage_provider=provider,
        )
        assert error is None
        tool_names = extension_loader.snapshot()["tools"]
        assert len(tool_names) == 9
        assert any(name.endswith("_create_reminder") for name in tool_names)
        assert any(name.endswith("_create_scheduled_task") for name in tool_names)
        assert not any(name.endswith("_create_schedule") for name in tool_names)
        create_name = next(name for name in tool_names if name.endswith("_create_reminder"))
        create_tool = extension_loader.get_tool(create_name)
        assert create_tool is not None
        result = json.loads(
            create_tool["handler"](
                SimpleNamespace(current_chat_id=42, current_task_source="browser"),
                message="Lunch",
                recurrence="once",
                in_minutes=10,
            )
        )
        assert result["state"] == "active"
        assert len(provider.list_keys("state", "skills/tasks-scheduler/schedules/")) == 1
    finally:
        extension_loader.unload_extension("tasks-scheduler")


def test_once_reminder_is_active_immediately_and_sent_once_without_llm(skill_modules, schedule_store) -> None:
    clock = {"now": _ts(2026, 7, 10, 8, 0)}
    api = _FakeApi()
    store = schedule_store
    scheduler = skill_modules.service.SchedulerService(
        store,
        api,
        timezone_name="Europe/Moscow",
        now_fn=lambda: clock["now"],
    )

    created = scheduler.create_reminder(
        SimpleNamespace(current_chat_id=42, current_task_source="browser"),
        message="Lunch time",
        recurrence="once",
        in_minutes=10,
    )

    assert created["state"] == "active"
    assert created["execution_kind"] == "reminder"
    assert created["needs_confirmation"] is False
    clock["now"] += 11 * 60
    assert scheduler.tick() == 1
    assert scheduler.tick() == 0
    assert api.injected == []
    assert api.sent == [
        {
            "text": "Lunch time",
            "chat_id": 42,
            "source": "browser",
            "task_id": f"{created['id']}:{int(clock['now'] - 60)}",
        }
    ]


def test_recurring_reminder_requires_confirmation_then_sends_directly(skill_modules, schedule_store) -> None:
    clock = {"now": _ts(2026, 7, 10, 8, 0)}
    api = _FakeApi()
    scheduler = skill_modules.service.SchedulerService(
        schedule_store,
        api,
        timezone_name="Europe/Moscow",
        now_fn=lambda: clock["now"],
    )

    created = scheduler.create_reminder(
        SimpleNamespace(current_chat_id=7, current_task_source="dialogs"),
        message="Stand up",
        recurrence="daily",
        time="09:00",
    )

    assert created["state"] == "pending"
    assert created["needs_confirmation"] is True
    active = scheduler.confirm_schedule(created["id"], revision=created["revision"])
    assert active["state"] == "active"
    clock["now"] = _ts(2026, 7, 10, 9, 1)

    assert scheduler.tick() == 1
    assert api.injected == []
    assert api.sent[0]["text"] == "Stand up"
    assert api.sent[0]["source"] == "dialogs"


def test_agent_task_always_requires_confirmation(skill_modules, schedule_store) -> None:
    scheduler = skill_modules.service.SchedulerService(
        schedule_store,
        _FakeApi(),
        timezone_name="Europe/Moscow",
        now_fn=lambda: _ts(2026, 7, 10, 8, 0),
    )

    created = scheduler.create_scheduled_task(
        SimpleNamespace(current_chat_id=7, current_task_source="browser"),
        instruction="Check mail and summarize new messages",
        recurrence="once",
        in_minutes=5,
    )

    assert created["execution_kind"] == "agent_task"
    assert created["state"] == "pending"
    assert created["needs_confirmation"] is True


def test_dedup_includes_execution_kind(skill_modules, schedule_store) -> None:
    scheduler = skill_modules.service.SchedulerService(
        schedule_store,
        _FakeApi(),
        timezone_name="Europe/Moscow",
        now_fn=lambda: _ts(2026, 7, 10, 8, 0),
    )
    ctx = SimpleNamespace(current_chat_id=7, current_task_source="browser")

    first = scheduler.create_reminder(ctx, message="Check mail", recurrence="once", in_minutes=5)
    duplicate = scheduler.create_reminder(ctx, message="Check mail", recurrence="once", in_minutes=5)
    agent_task = scheduler.create_scheduled_task(
        ctx,
        instruction="Check mail",
        recurrence="once",
        in_minutes=5,
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["duplicate"] is True
    assert agent_task["id"] != first["id"]
    assert len(scheduler.store.list_schedules()) == 2


def test_legacy_schedule_without_execution_kind_is_agent_task(skill_modules) -> None:
    legacy = _schedule("f1433cb7-0bea-4860-84c0-5f95d152f563")
    legacy.pop("execution_kind")

    normalized = skill_modules.core.normalize_definition(legacy)

    assert normalized["execution_kind"] == "agent_task"


def test_recreated_service_does_not_repeat_claimed_slot(skill_modules, schedule_store_factory) -> None:
    clock = {"now": _ts(2026, 7, 10, 8, 0)}
    api = _FakeApi()

    first_service = skill_modules.service.SchedulerService(
        schedule_store_factory(), api, timezone_name="Europe/Moscow", now_fn=lambda: clock["now"]
    )
    created = first_service.create_reminder(
        SimpleNamespace(current_chat_id=42, current_task_source="browser"),
        message="Lunch time",
        recurrence="once",
        in_minutes=10,
    )
    clock["now"] += 11 * 60
    assert first_service.tick() == 1

    recreated_service = skill_modules.service.SchedulerService(
        schedule_store_factory(), api, timezone_name="Europe/Moscow", now_fn=lambda: clock["now"]
    )

    assert recreated_service.tick() == 0
    assert len(api.sent) == 1
    stored = schedule_store_factory().get_schedule(created["id"])
    assert stored is not None and stored.value["state"] == "completed"
