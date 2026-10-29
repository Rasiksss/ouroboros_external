"""ADR-0098: HTTP-инструменты → граф → Case, без отчёта."""
import asyncio
import re

from tool_harness import OWNER, Plugin, ctx_of, open_run

from finengine.assistant import goal_tools
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_opening import LoopOpening
from finengine.assistant.tool_api import ToolApi
from finengine.config import settings
from finengine.contracts import DigestOutput


class Gateway:
    def root_known(self, run_id, task_id):
        return True


class Digest:
    def __init__(self):
        self.calls = 0
        self.status = "complete"
        self.text = "# Дайджест\n\n[Источник](https://cbr.ru) · проверенный выпуск"
        self.slow = False
        self.warnings: list[str] = []

    async def __call__(self, request):
        self.calls += 1
        if self.slow:
            await asyncio.sleep(0.2)
        return DigestOutput(
            window_start=request.window_start, window_end=request.window_end, collected_at=request.window_end,
            markdown=self.text, status=self.status, warnings=self.warnings,
        )


def move(ref="g1", hours=168):
    return {"objective": "дайджест", "reason": "запрос пользователя", "goals": [
        {"ref": ref, "scenario": "market_digest", "parameters": {"since_hours": hours}},
    ]}


async def setup(base, **record):
    run = open_run(doc_ids=(), message="дайджест за неделю", **record)
    desk, digest = LoopDesk(), Digest()
    api = ToolApi(Gateway(), LoopOpening(desk, market_store=base.store, digest_runner=digest), desk)
    return run, desk, digest, Plugin(api), ctx_of(run.run_id)


async def open_market(plugin, ctx):
    response = await plugin.call("aifa_open", ctx, {"mode": "market"})
    while response.text.startswith("ЖДИ job="):
        job = re.search(r"job=(\w+)", response.text)[1]
        response = await plugin.call("aifa_wait", ctx, {"job_id": job, "wait_sec": 1})
    return response


async def test_market_opens_without_pdf_and_preserves_report_default(base):
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        rejected = await plugin.call("aifa_open", ctx)
        assert rejected.text.startswith("ОТКАЗ нет_документа")
        # Отказ открытия остаётся идемпотентным внутри хода; market — новый ход.
    finally:
        await plugin.close()
        await run.end()
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        opened = await open_market(plugin, ctx)
        assert opened.text.startswith("ГОТОВО"), opened.text
        assert desk.live(run.run_id).session.run_passport is None
        assert desk.live(run.run_id).origin.document_id is None
        assert digest.calls == 0
        conflict = await plugin.call("aifa_open", ctx)
        assert conflict.text.startswith("ОТКАЗ неверные_аргументы")
    finally:
        await plugin.close()
        await run.end()


async def test_digest_is_a_graph_result_readable_from_case_and_not_recollected(base):
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        assert (await open_market(plugin, ctx)).text.startswith("ГОТОВО")
        reply = await plugin.call("aifa_move", ctx, move())
        assert "complete: DigestOutput" in reply.text, reply.text
        rows = base.store.list_artifacts(7, "project-1")
        output = next(p for row in rows for p in row["payloads"] if isinstance(p, DigestOutput))
        read = await plugin.call("aifa_state", ctx, {"artifact_ids": [output.output_id]})
        assert "проверенный выпуск" in read.text
        assert (await plugin.call("aifa_move", ctx, move())).text.startswith("ГОТОВО")
        assert digest.calls == 1
        fresh = await plugin.call("aifa_move", ctx, move(ref="g2", hours=48))
        assert "complete: DigestOutput" in fresh.text
        assert digest.calls == 2
        outputs = [p for row in base.store.list_artifacts(7, "project-1") for p in row["payloads"] if isinstance(p, DigestOutput)]
        assert len({p.output_id for p in outputs}) == 2
        assert sorted((p.window_end-p.window_start).total_seconds()/3600 for p in outputs) == [48,168]
        changed = await plugin.call("aifa_move", ctx, move(hours=48))
        assert changed.text.startswith("ОТКАЗ"), changed.text
    finally:
        await plugin.close()
        await run.end()


async def test_long_digest_is_waited_through_aifa_wait(base, monkeypatch):
    monkeypatch.setattr(settings, "ouroboros_goal_wait_sec", 0.02)
    monkeypatch.setattr(goal_tools, "WAIT_MIN_SEC", 0)
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        digest.slow = True
        response = await plugin.call("aifa_move", ctx, move())
        assert response.text.startswith("ЖДИ job="), response.text
        job = re.search(r"job=(\w+)", response.text)[1]
        result = await plugin.call("aifa_wait", ctx, {"job_id": job, "wait_sec": 1})
        assert "complete: DigestOutput" in result.text, result.text
        assert digest.calls == 1
    finally:
        await plugin.close()
        await run.end()


async def test_report_scenario_is_refused_in_market(base):
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        result = await plugin.call("aifa_move", ctx, {
            "objective": "форма", "reason": "проверка режима", "goals": [{"ref": "g1", "scenario": "form", "role": "other"}],
        })
        assert result.text.startswith("ОТКАЗ"), result.text
        assert digest.calls == 0
    finally:
        await plugin.close()
        await run.end()


async def test_delivery_replay_uses_the_same_job_and_rejects_changed_payload(base, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_database_url", f"sqlite:///{tmp_path / 'operations.db'}")
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        ctx["operation_id"] = "move-delivery-1"
        replies = await asyncio.gather(*(plugin.call("aifa_move", ctx, move()) for _ in range(2)))
        assert replies[0].text == replies[1].text
        assert "complete: DigestOutput" in replies[0].text
        assert digest.calls == 1
        conflicting = await plugin.call("aifa_move", ctx, move(hours=48))
        assert conflicting.text.startswith("ОТКАЗ неверные_аргументы")
    finally:
        await plugin.close()
        await run.end()


async def test_market_cycle_restores_without_loading_a_document(base, monkeypatch):
    from finengine.skills.news import market
    monkeypatch.setattr(market, "get_case_store", lambda: base.store)
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        await plugin.call("aifa_move", ctx, move())
        desk._open.clear()
        state = await plugin.call("aifa_state", ctx)
        assert state.text.startswith("ГОТОВО"), state.text
        assert desk.live(run.run_id).origin.mode == "market"
        assert desk.live(run.run_id).session.run_passport is None
        again = await plugin.call("aifa_move", ctx, move())
        assert "complete: DigestOutput" in again.text
        assert digest.calls == 1
        finished = await plugin.call("aifa_finish", ctx, {"action": "finalize"})
        assert finished.text.startswith("ПРИНЯТО"), finished.text
    finally:
        await plugin.close()
        await run.end()


async def test_partial_digest_is_readable_but_does_not_mark_goal_obtained(base):
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        digest.status = "partial"
        digest.warnings = ["Недоступны новостные источники: secret_source"]
        reply = await plugin.call("aifa_move", ctx, move())
        assert "partial: DigestOutput" in reply.text and "неполный" in reply.text
        assert "выжимка на 5–8 строк" in reply.text and "без перечня источников" in reply.text
        assert "secret_source" not in reply.text  # перечень недоступного — только по просьбе пользователя
        assert desk.live(run.run_id).session.goal_status["g1"].startswith("отказ")
        assert len(base.store.list_artifacts(7, "project-1")) == 1
    finally:
        await plugin.close()
        await run.end()


async def test_long_snapshot_can_be_read_in_full_without_regenerating(base):
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        digest.text = "Начало " + "Очень длинный выпуск. " * 2000 + " КОНЕЦ"
        await plugin.call("aifa_move", ctx, move())
        output = base.store.list_artifacts(7, "project-1")[0]["payloads"][0]
        from finengine.skills.spec.dynamic_run import artifact_content
        original = artifact_content(output)
        offset, chunks = 0, []
        while True:
            response = await plugin.call("aifa_state", ctx, {"artifact_ids": [output.output_id], "artifact_offset": offset})
            assert len(response.text) <= settings.ouroboros_result_limit
            chunk = response.text.split("\n", 2)[2]
            chunks.append(chunk.rsplit("\nПродолжение:", 1)[0].rsplit("\nnext_offset=", 1)[0])
            found = re.search(r"next_offset=(\d+|null)", response.text)
            if found[1] == "null":
                break
            offset = int(found[1])
        assert "".join(chunks) == original
        assert digest.calls == 1
    finally:
        await plugin.close()
        await run.end()


async def test_stop_cancels_running_collection_and_never_publishes_late_output(base, monkeypatch):
    monkeypatch.setattr(settings, "ouroboros_goal_wait_sec", 0.01)
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        opened = await open_market(plugin, ctx)
        if opened.text.startswith("ЖДИ job="):
            job = re.search(r"job=(\w+)", opened.text)[1]
            await plugin.call("aifa_wait", ctx, {"job_id": job, "wait_sec": 1})
        digest.slow = True
        result = await plugin.call("aifa_move", ctx, move())
        assert result.text.startswith("ЖДИ job=")
        await run.end()
        await asyncio.sleep(0.4)
        assert base.store.list_artifacts(7, "project-1") == []
    finally:
        await plugin.close()
        if not run._release.is_set():
            await run.end()


async def test_same_ref_on_two_restored_replicas_collects_once(base, monkeypatch):
    from finengine.skills.news import market
    monkeypatch.setattr(market, "get_case_store", lambda: base.store)
    run, desk, digest, plugin, ctx = await setup(base)
    second = None
    try:
        await open_market(plugin, ctx)
        other_desk = LoopDesk()
        other_api = ToolApi(Gateway(), LoopOpening(other_desk, market_store=base.store, digest_runner=digest), other_desk)
        second = Plugin(other_api)
        await second.call("aifa_state", ctx)
        other_desk.live(run.run_id).session.run_goals.market.runner = digest
        digest.slow = True
        replies = await asyncio.gather(plugin.call("aifa_move", ctx, move()), second.call("aifa_move", ctx, move()))
        assert all("complete: DigestOutput" in reply.text for reply in replies)
        assert digest.calls == 1
        assert len(base.store.list_artifacts(7, "project-1")) == 1
    finally:
        if second:
            await second.close()
        await plugin.close()
        await run.end()


async def test_replica_restored_after_collection_started_waits_for_the_same_result(base, monkeypatch):
    from finengine.skills.news import market
    monkeypatch.setattr(market, "get_case_store", lambda: base.store)
    run, desk, digest, plugin, ctx = await setup(base)
    second = None
    try:
        await open_market(plugin, ctx)
        digest.slow = True
        first = asyncio.create_task(plugin.call("aifa_move", ctx, move()))
        while digest.calls == 0:
            await asyncio.sleep(0.001)
        other_desk = LoopDesk()
        second = Plugin(ToolApi(Gateway(), LoopOpening(other_desk, market_store=base.store, digest_runner=digest), other_desk))
        replay = await second.call("aifa_move", ctx, move())
        original = await first
        assert "complete: DigestOutput" in replay.text, replay.text
        assert "complete: DigestOutput" in original.text
        assert digest.calls == 1
    finally:
        if second:
            await second.close()
        await plugin.close()
        await run.end()


async def test_concurrent_open_cannot_replace_the_reserved_mode(base, monkeypatch):
    run, desk, digest, plugin, ctx = await setup(base)
    entered, release = asyncio.Event(), asyncio.Event()
    actual = LoopOpening.market
    async def held(opening, record, agent_id=""):
        entered.set()
        await release.wait()
        return await actual(opening, record, agent_id)
    monkeypatch.setattr(LoopOpening, "market", held)
    try:
        first = asyncio.create_task(open_market(plugin, ctx))
        await entered.wait()
        conflicting = await plugin.call("aifa_open", ctx, {"mode": "report"})
        assert conflicting.text.startswith("ОТКАЗ неверные_аргументы"), conflicting.text
        release.set()
        assert (await first).text.startswith("ГОТОВО")
        assert desk.live(run.run_id).origin.mode == "market"
    finally:
        release.set()
        await plugin.close()
        await run.end()


async def test_lost_opening_is_refused_instead_of_waiting_forever(base):
    from finengine.store import state
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        state.claim("loop_opening", owner=7, key=run.run_id, doc={"mode": "market", "token": "dead-process", "expires_at": 0})
        response = await plugin.call("aifa_open", ctx, {"mode": "market"})
        assert response.text.startswith("ОТКАЗ задание_не_найдено"), response.text
        assert digest.calls == 0
        assert desk.live(run.run_id) is None
    finally:
        await plugin.close()
        await run.end()


async def test_failed_tool_execution_records_a_result_for_the_operation(base, monkeypatch, tmp_path):
    from finengine.assistant.loop_tools import MoveTool
    from finengine.store import state
    monkeypatch.setattr(settings, "state_database_url", f"sqlite:///{tmp_path / 'failed.db'}")
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        monkeypatch.setattr(MoveTool, "_execute", lambda self, loop, args: (_ for _ in ()).throw(RuntimeError("сбой")))
        ctx["operation_id"] = "move-fails"
        await plugin.call("aifa_move", ctx, move())
        owner = str(int(OWNER))
        docs = [state.get("tool_operation", owner=owner, key=k) for k in state.keys("tool_operation", owner=owner)]
        assert docs and all(doc["result"] and doc["result"].startswith("СБОЙ_СЛУЖБЫ") for doc in docs), docs
    finally:
        await plugin.close()
        await run.end()


async def test_abandoned_claim_is_not_waited_for_after_its_lease(base, monkeypatch, tmp_path):
    import hashlib
    import time
    from datetime import datetime, timezone
    from finengine.store import state
    monkeypatch.setattr(settings, "state_database_url", f"sqlite:///{tmp_path / 'lease.db'}")
    run, desk, digest, plugin, ctx = await setup(base)
    try:
        await open_market(plugin, ctx)
        runner = desk.live(run.run_id).session.run_goals.market
        key = hashlib.sha256(f"{runner.case_id}:{runner.cycle_id}:g1".encode()).hexdigest()
        state.put("market_ref", owner=str(runner.owner_id), key=key, doc={
            "token": "dead-process", "parameters": {"since_hours": 168}, "beat": time.time() - 3600,
            "window_end": datetime.now(timezone.utc).isoformat(), "done": False, "ok": False,
            "summary": "Сбор принят другой репликой или прерван; новый запуск требует нового ref",
        })
        started = time.monotonic()
        reply = await plugin.call("aifa_move", ctx, move())
        assert time.monotonic() - started < 10
        assert "прерван" in reply.text and digest.calls == 0, reply.text
    finally:
        await plugin.close()
        await run.end()
