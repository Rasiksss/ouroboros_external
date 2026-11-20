from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_switch_default_and_boolean_env():
    from ouroboros.storage.settings.schema import AppSettings
    assert AppSettings().OUROBOROS_MEMORYLESS_MODE is False
    assert AppSettings.model_validate({"OUROBOROS_MEMORYLESS_MODE": "true"}).OUROBOROS_MEMORYLESS_MODE is True


def test_prompt_sections_do_not_read_persistent_memory(monkeypatch):
    from ouroboros.agent import context
    inputs = context.SectionInputs(env=None, task={"memoryless_mode": True}, scope=None,
                                   skill_surface=None, execution_memory_repository=None)
    for name in ("get_memory", "get_scratchpad", "list_recent_owner_messages"):
        monkeypatch.setattr(context, name, Mock(side_effect=AssertionError("historical read")))
    assert inputs.headers == []
    assert inputs.recent_chat is None
    names = {section.name for section in context.prompt_sections(inputs)}
    assert not names & {"memory", "knowledge", "scratchpad", "recalled_memories", "recent_chat", "working_retrieval"}
    assert {"skills", "runtime", "base_prompt"} <= names


def test_normal_prompt_still_has_memory_sections():
    from ouroboros.agent import context
    inputs = SimpleNamespace(task={"memoryless_mode": False})
    assert {"memory", "scratchpad", "recent_chat", "working_retrieval"} <= {
        section.name for section in context.prompt_sections(inputs)}


def test_tool_surface_hides_history_but_keeps_current_task_tools():
    from ouroboros.tools.surface import ProviderToolSurface
    descriptors = [SimpleNamespace(name=name, origin="builtin") for name in (
        "chat_history", "memory_read", "recent_tasks", "data_read", "get_task_result", "schedule_task")]
    catalog = SimpleNamespace(descriptors=lambda scope: descriptors,
                              find=lambda name, scope: next((d for d in descriptors if d.name == name), None))
    surface = ProviderToolSurface(catalog, set(), None, memoryless_mode=True)
    assert {d.name for d in surface.catalog()} == {"get_task_result", "schedule_task"}
    assert surface.find("chat_history") is None


@pytest.mark.asyncio
async def test_direct_history_dispatch_is_refused_before_provider_lookup():
    from ouroboros.tools.dispatch import execute_tool
    ctx = SimpleNamespace(task_id="root", memoryless_mode=True)
    catalog = Mock()
    result = await execute_tool(catalog, ctx, "chat_history", {}, origin="builtin")
    assert "MEMORYLESS" in result
    catalog.find.assert_not_called()


def test_previous_task_refused_current_children_allowed(monkeypatch):
    from ouroboros.agent import memory_policy
    scope = SimpleNamespace(owner_id="owner", agent_id="agent")
    ctx = SimpleNamespace(memoryless_mode=True, task_id="root", root_task_id="root", scope=scope)
    records = {"child": SimpleNamespace(owner_id="owner", agent_id="agent", parent_task_id="root"),
               "old": SimpleNamespace(owner_id="owner", agent_id="agent", parent_task_id="")}
    repo = SimpleNamespace(get_task=records.get)
    monkeypatch.setattr(memory_policy, "_tasks", lambda: repo)
    assert memory_policy.tool_refusal(ctx, "get_task_result", {"task_id": "child"}) is None
    assert memory_policy.tool_refusal(ctx, "get_task_result", {"task_id": "root"}) is None
    assert "MEMORYLESS" in memory_policy.tool_refusal(ctx, "get_task_result", {"task_id": "old"})
    assert "MEMORYLESS" in memory_policy.tool_refusal(ctx, "schedule_task", {"parent_task_id": "old"})
    ctx.memoryless_mode = False
    assert memory_policy.tool_refusal(ctx, "get_task_result", {"task_id": "old"}) is None


def test_fresh_transcript_skips_memory_recall_and_keeps_current_input(monkeypatch):
    from ouroboros.agent import core, memory_recall
    from ouroboros.storage.core_memory import memory
    from ouroboros.storage.settings import store
    from ouroboros.storage.settings.schema import AppSettings
    monkeypatch.setattr(store, "load_settings", lambda: AppSettings())
    monkeypatch.setattr(memory, "get_memory", Mock(side_effect=AssertionError("must not read")))
    monkeypatch.setattr(memory_recall, "recall_relevant_memories", Mock(side_effect=AssertionError("must not recall")))
    monkeypatch.setattr(core, "build_llm_messages", lambda **kw: ([{"role": "user", "content": kw["task"]["text"]}], {}))
    agent = SimpleNamespace(_emit_live_log=lambda *a, **kw: None, llm=Mock(), env=None,
                            execution_memory_repository=None)
    task = {"id": "new", "text": "current request", "memoryless_mode": True}
    result = core.OuroborosAgent._assemble_opening_transcript(agent, task, scope=None, skill_surface=None, ctx=None, cancellation=None)
    assert result[-1]["content"] == "current request"
    memory.get_memory.assert_not_called()
    memory_recall.recall_relevant_memories.assert_not_called()


def test_dialogs_registration_passes_switch(monkeypatch):
    import json

    import httpx

    from new_core.identity import OwnerScope, SurfaceId
    from transport.dialogs import aifa_ingress

    configured = SimpleNamespace(OUROBOROS_MEMORYLESS_MODE=True, AIFA_ENGINE_URL="http://aifa:8080",
                                 AIFA_ENGINE_TOKEN=SimpleNamespace(get_secret_value=lambda: "token"),
                                 OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS=["distribution:aifa-engine"])
    sent = []

    def respond(request):
        from email import policy
        from email.parser import BytesParser

        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode() + request.content)
        sent.append(json.loads(next(message.iter_parts()).get_payload(decode=True)))
        return httpx.Response(200, json={"run_id": "run"})

    original = httpx.Client
    monkeypatch.setattr(aifa_ingress.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(respond)))
    scope = OwnerScope(owner_id="owner", agent_id="agent", surface_id=SurfaceId.DIALOGS, chat_id="chat")
    assert aifa_ingress.register_turn(configured, scope, "message", "current", [], None) == "run"
    assert sent[0]["memoryless"] is True


def test_memoryless_task_scope_check_refuses_other_owner(monkeypatch):
    from ouroboros.agent import memory_policy
    ctx = SimpleNamespace(memoryless_mode=True, task_id="root", root_task_id="root",
                          scope=SimpleNamespace(owner_id="owner", agent_id="agent"))
    foreign = SimpleNamespace(owner_id="other", agent_id="agent", parent_task_id="root")
    monkeypatch.setattr(memory_policy, "_tasks", lambda: SimpleNamespace(get_task=lambda task_id: foreign))
    assert "MEMORYLESS" in memory_policy.tool_refusal(ctx, "get_task_result", {"task_id": "foreign"})


def test_rendered_llm_messages_exclude_historical_snapshots(monkeypatch):
    import json

    from ouroboros.agent import context

    for name in ("get_memory", "get_scratchpad", "list_recent_owner_messages"):
        monkeypatch.setattr(context, name, Mock(side_effect=AssertionError("historical read")))
    monkeypatch.setattr(context, "_section_base_prompt", lambda inputs: "base instructions")
    monkeypatch.setattr(context, "_section_deployment_prompt", lambda inputs: "deployment instructions")
    monkeypatch.setattr(context, "_section_skills", lambda inputs: "current skills")
    monkeypatch.setattr(context, "_section_ready_skills", lambda inputs: "")
    monkeypatch.setattr(context, "build_runtime_section", lambda task, scope: "runtime")
    task = {"id": "new", "text": "CURRENT_REQUEST", "memoryless_mode": True, "detached": True,
            "committed_summary_snapshot": "SECRET_OLD_HISTORY", "inherited_context_seed": "SECRET_OLD_SCRATCH"}
    messages, _ = context.build_llm_messages(
        env=None, task=task, scope=None, skill_surface=None, execution_memory_repository=None,
        recalled_memories=[SimpleNamespace(content="SECRET_OLD_MEMORY")])
    rendered = json.dumps(messages)
    assert "CURRENT_REQUEST" in rendered and "SECRET_OLD" not in rendered
    assert "Memoryless mode" in rendered and "current skills" in rendered
