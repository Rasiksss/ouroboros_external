"""Рыночный сценарий в общем графе и Case службы (ADR-0098)."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
import threading
import time
from typing import Callable, Protocol

from finengine.contracts import DigestOutput, DigestParameters, GoalClause, GoalIR, PlanPolicy
from finengine.policy.sandbox_profiles import external_enrichment, register_dynamic_profile
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs
from finengine.skills.spec.dynamic_run import LoopSession, RunLimits, ScenarioResult
from finengine.skills.spec.graph_executor import ExecutionBudget, ExecutionContext, RunInputs
from finengine.skills.spec.loop_state import LoopOrigin, restore_state
from finengine.skills.spec.target import OutputSpec, Target
from finengine.store.cases import SqlCaseStore, get_case_store
from finengine.store import state


# Исполнитель части раз в HEARTBEAT_SEC продлевает claim; без продления дольше LEASE_SEC claim считается брошенным.
HEARTBEAT_SEC = 10.0
LEASE_SEC = 45.0


@dataclass(frozen=True)
class DigestRequest:
    window_start: datetime
    window_end: datetime
    stopped: Callable[[], bool]
    progress: Callable[[dict], None]
    trace_id: str
    agent_id: str


class DigestRunner(Protocol):
    async def __call__(self, request: DigestRequest) -> DigestOutput: ...


async def run_bounded(runner: DigestRunner, request: DigestRequest) -> DigestOutput:
    task = asyncio.create_task(runner(request))
    try:
        while not task.done():
            if request.stopped():
                raise RuntimeError("Сбор остановлен или исчерпан срок цикла")
            await asyncio.wait({task}, timeout=0.2)
        if request.stopped():
            raise RuntimeError("Сбор остановлен до публикации результата")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@dataclass
class MarketGoalRunner:
    owner_id: int | str
    case_id: str
    cycle_id: str
    trace_id: str
    agent_id: str
    runner: DigestRunner
    session: LoopSession | None = None

    def refusal(self, clause: GoalClause) -> str | None:
        return None if clause.intent_kind == "market_digest" else "режим market принимает только market_digest"

    def execute_atom(self, args: NodeArgs, ctx: ExecutionContext) -> tuple[DigestOutput]:
        session = self.session
        if session is None:
            raise RuntimeError("рыночный цикл не открыт")
        end = datetime.fromisoformat(args.extra["window_end"])
        request = DigestRequest(
            window_start=end - timedelta(hours=int(args.extra["since_hours"])), window_end=end,
            stopped=lambda: bool(session.is_cancelled and session.is_cancelled()) or session.limit() is not None,
            progress=session._emit, trace_id=self.trace_id, agent_id=self.agent_id,
        )
        return (asyncio.run(run_bounded(self.runner, request)).model_copy(update={"run_id": ctx.run_id}),)

    def __call__(self, clauses: list[GoalClause]) -> ScenarioResult:
        session = self.session
        if session is None:
            raise RuntimeError("рыночный цикл не открыт")
        lines: list[str] = []
        ok = True
        for clause in clauses:
            ref = clause.goal_ref
            prior = session.market_outcomes.get(ref)
            if prior is not None and prior.get("done", True):
                lines.append(prior["summary"])
                ok = ok and prior["ok"]
                continue
            params = DigestParameters.model_validate(clause.parameters)
            token = uuid.uuid4().hex
            key = hashlib.sha256(f"{self.case_id}:{self.cycle_id}:{ref}".encode()).hexdigest()
            claim = state.claim("market_ref", owner=str(self.owner_id), key=key, doc={
                "token": token, "parameters": params.model_dump(), "beat": time.time(),
                "window_end": session.market_windows.get(ref) or datetime.now(timezone.utc).isoformat(),
                "done": False, "ok": False, "summary": "Сбор принят другой репликой или прерван; новый запуск требует нового ref",
            })
            if claim["parameters"] != params.model_dump():
                lines.append(f"Конфликт параметров части {ref}; нужен новый ref")
                ok = False
                continue
            if claim["token"] != token:
                deadline = datetime.fromisoformat(claim["window_end"]) + timedelta(seconds=1200)
                while not claim.get("done") and datetime.now(timezone.utc) < deadline:
                    if time.time() - claim.get("beat", 0) > LEASE_SEC:
                        break
                    _check_active(session)
                    time.sleep(0.1)
                    claim = state.get("market_ref", owner=str(self.owner_id), key=key) or claim
                session.market_outcomes[ref] = {"ok": claim["ok"], "summary": claim["summary"]}
                lines.append(claim["summary"])
                ok = ok and claim["ok"]
                continue
            with session.state_lock:
                end = session.market_windows.setdefault(ref, claim["window_end"])
                session.market_outcomes[ref] = {"done": False, "ok": False, "summary": "Сбор прерван; новый запуск требует нового ref"}
                session.market_checkpoint()
            params = DigestParameters.model_validate(clause.parameters)
            alive = threading.Event()
            beat = threading.Thread(
                target=_renew_claim, args=(alive, str(self.owner_id), key, token), name="market-claim-beat", daemon=True,
            )
            beat.start()
            extra = {"cycle_id": self.cycle_id, "ref": ref, "window_end": end, "since_hours": str(params.since_hours)}
            try:
                goal = GoalIR(**clause.model_dump())
                target = Target(output_type="DigestOutput", entity_ref=f"market:{self.cycle_id}:{ref}", params={"document_scope": "none"})
                planned = session.runtime.plan(
                    owner_id=self.owner_id, case_id=self.case_id, goal_ir=goal, target=target, root_extra=extra,
                )
                executed = session.runtime.execute(
                    owner_id=self.owner_id, case_id=self.case_id, plan_run_id=planned.record.plan_run_id,
                    inputs=RunInputs(), event_sink=session._emit,
                    commit_guard=partial(_check_active, session),
                )
                outputs = [value for values in executed.evidence_by_key.values() for value in values if isinstance(value, DigestOutput)]
                if not executed.succeeded or not outputs:
                    raise RuntimeError("; ".join(node.error or node.status for node in executed.errors) or "Дайджест не получен")
                result = outputs[0]
                accepted = result.status != "partial"
                summary = f"{result.status}: DigestOutput {result.output_id}"
                if result.warnings:
                    summary += "\n" + "\n".join(result.warnings)
            except Exception as exc:
                accepted, summary = False, f"Дайджест не выполнен: {exc}"
            finally:
                alive.set()
                beat.join(timeout=HEARTBEAT_SEC)
            state.put("market_ref", owner=str(self.owner_id), key=key, doc={**claim, "done": True, "ok": accepted, "summary": summary})
            with session.state_lock:
                session.market_outcomes[ref] = {"done": True, "ok": accepted, "summary": summary}
                session.market_checkpoint()
            lines.append(summary)
            ok = ok and accepted
        return ScenarioResult(ok, "\n".join(lines))


def _renew_claim(stop: threading.Event, owner: str, key: str, token: str) -> None:
    """Продлевать claim, пока сбор идёт; чужой или завершённый claim не трогаем."""
    def renew(doc: dict | None) -> dict:
        if doc is None or doc.get("token") != token or doc.get("done"):
            return doc or {}
        return {**doc, "beat": time.time()}

    while not stop.wait(HEARTBEAT_SEC):
        try:
            state.mutate("market_ref", owner=owner, key=key, изменить=renew)
        except Exception:  # продление не должно ронять сбор; без него claim лишь устареет
            continue


def market_step_check(args: NodeArgs) -> str:
    return "market.digest запускается только сценарием market_digest"


def _check_active(session: LoopSession) -> None:
    if (session.is_cancelled and session.is_cancelled()) or session.limit() is not None:
        raise RuntimeError("Сбор остановлен до сохранения результата")


def open_market_loop(
    *, owner_id: int | str, case_id: str, message: str, hand_over: Callable,
    event_sink: Callable | None, trace_id: str = "", agent_id: str = "",
    store: SqlCaseStore | None = None, runner: DigestRunner | None = None,
    origin: LoopOrigin | None = None, saved: dict | None = None,
) -> LoopSession:
    # Разрыв цикла: production_runtime → market → service → market.DigestRequest.
    from finengine.skills.production_runtime import GoalChannel, SwitchSink
    from finengine.skills.news.service import ServiceDigest, catalogue_hosts, catalogue_version

    cycle_id = origin.target.scope_id if origin is not None else uuid.uuid4().hex
    channel = MarketGoalRunner(owner_id, case_id, cycle_id or "", trace_id, agent_id, runner or ServiceDigest())
    profile = register_dynamic_profile("market_digest", external_enrichment(allowed_hosts=catalogue_hosts()))
    atom = AtomDef(
        name="market.digest", version=catalogue_version(), produces=(OutputSpec("DigestOutput"),),
        sandbox_profile=profile, execution_kind="io", cache_policy="run_scoped", cost_class="model_call",
        executor=channel.execute_atom, step_check=market_step_check, key_fields=("cycle_id", "ref", "window_end", "since_hours"),
    )
    runtime = CaseRuntime(
        store=store or get_case_store(), registry=AtomRegistry([atom]), policy=PlanPolicy(policy_version="market.v1"),
        budget=ExecutionBudget(max_wall_seconds=1200, max_retries=0),
    )
    runtime.ensure_case(owner_id=owner_id, case_id=case_id)
    goal = origin.goal if origin else GoalIR(raw_query=message, intent_kind="market_digest")
    target = origin.target if origin else Target(output_type="DigestOutput", scope_id=cycle_id, params={"document_scope": "none"})
    session = LoopSession(
        runtime=runtime, owner_id=owner_id, case_id=case_id, goal=goal, target=target,
        inputs=RunInputs(), run_passport=None, event_sink=SwitchSink(event_sink), entry_stage="agent",
        limits=RunLimits(max_wall_seconds=1200),
        run_goals=GoalChannel(context=None, document_text="", role="", event_sink=event_sink, market=channel),
    )
    channel.session = session
    session.open(deterministic_error="")
    if saved is not None:
        restore_state(session, saved)
    hand_over(session, origin or LoopOrigin(
        owner_id=owner_id, case_id=case_id, document_id=None, goal=goal, target=target,
        mode="market", trace_id=trace_id, entry_stage="agent", agent_id=agent_id,
    ))
    return session
