"""`GraphExecutor` — исполняет уже построенный `PlanGraph` (A2, docs/build-plan/
13-srez-03-graph-planner-executor.md). Отвечает ТОЛЬКО за исполнение: порядок
и достижимость уже решены `GraphPlanner`, `GraphExecutor` их не пересматривает.

Предусловие: `graph` уже признан достижимым (`PlanBuildResult.feasibility.
feasible`) вызывающим кодом. Исполнение заведомо недостижимого плана — ошибка
вызывающего, не состояние, которое `GraphExecutor` обязан красиво отработать.
"""
from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import repeat

from pydantic import BaseModel

from finengine.contracts import NodeKey, NodeRun, PlanGraph
from finengine.contracts.base import result_id_of
from finengine.contracts import RunPassport as _RunPassport
from finengine.policy.sandbox_profiles import (
    DYNAMIC_SANDBOX_PROFILES,
    SANDBOX_PROFILES,
    NetworkAccess,
    SandboxProfile,
)
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs, node_arg_value
from finengine.skills.spec.graph_planner import AvailableIndex, AvailableLeaf

# Автомат состояний (A2): `planned → ready → running → succeeded|failed|
# blocked|skipped`. `NodeRun.status` (contracts/execution.py) уже несёт
# `Literal` с этими семью значениями — здесь только имена терминалов, которыми
# оперирует планировщик готовности.
TERMINAL_FAILURE = ("failed", "blocked", "skipped")


class SandboxViolation(RuntimeError):
    """Атом заявляет требования (`execution_kind`), которых его собственный
    `sandbox_profile` не покрывает (A2.1.8). Поднимается ДО первого вызова
    `executor` — узел блокируется, не запускается «почти безопасно»."""


@dataclass(frozen=True)
class RunInputs:
    """Сырые входы одного прогона, НЕ смоделированные как узлы графа —
    например, уже распознанный текст документа. Атомы уровня «адаптер над
    существующим крупным компонентом» (`ingest.document` и подобные) читают
    их отсюда, а не откуда-то ещё.

    `pdf_bytes` — Срез 5a: сырые байты ОДНОГО документа прогона, вход
    `doc.textify` (он единственный, кто их читает; `document_text` остаётся
    отдельным входом для маршрутов, у которых текст уже распознан заранее и
    байтов PDF никогда не было — см. докстринг `graph_atoms.py`)."""

    document_text: str = ""
    pdf_bytes: bytes = b""
    role: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionBudget:
    """A2: «Общий `ExecutionBudget` ограничивает время, стоимость, число
    вызовов и глубину повторов». Стоимость в денежных единицах этот срез не
    считает (для этого нет ни одного реального модельного атома с ценником —
    появится вместе с `locate`/`read.value`, Срез 5a); число вызовов и время —
    считает по-настоящему, класс стоимости атома (`AtomDef.cost_class`)
    зарезервирован под будущий денежный бюджет."""

    max_wall_seconds: float = 300.0
    max_atom_calls: int = 5000
    max_retries: int = 2


class ExecutionContext:
    """То, что видит исполнитель одного атома: его собственные аргументы,
    `RunPassport` (если уже зафиксирован), сырые входы прогона и evidence уже
    успешно завершившихся зависимостей — доступ по их `NodeKey`, который сам
    атом получает через `PlanNode.requires` своего узла."""

    def __init__(
        self, *, run_id: str, run_passport: "_RunPassport | None", inputs: RunInputs,
        evidence_by_key: Mapping[NodeKey, tuple[BaseModel, ...]],
        requires: tuple[NodeKey, ...] = (),
    ):
        self.run_id = run_id
        self.run_passport = run_passport
        self.inputs = inputs
        self._evidence_by_key = evidence_by_key
        # `requires` своего узла (`PlanNode.requires`) — так исполнитель атома
        # достаёт evidence СВОИХ зависимостей, не зная заранее их `NodeKey`
        # (тот собран планировщиком, атом видит только `NodeArgs`). Для
        # пакетного вызова это ОБЪЕДИНЕНИЕ requires ВСЕХ узлов пакета (см.
        # `_run_batch`), не requires одного узла: до Среза 5a все пакетируемые
        # атомы (`extract.scalar`) имели одинаковые requires у каждого члена
        # пакета (один документ на пакет), и «requires первого узла» было
        # безобидным частным случаем объединения. `read.value` разрушил это
        # совпадение — каждый концепт в пакете требует СВОЙ узел `locate`
        # (`dynamic_requires` возвращает `locator:<concept>`), и без
        # объединения пакетный вызов видел бы кандидатов только одного
        # концепта из всех. Объединение — строгое надмножество прежнего
        # поведения: для одинаковых requires оно даёт тот же набор.
        self.requires = requires

    def dependency_evidence(self, node_key: NodeKey) -> tuple[BaseModel, ...]:
        return self._evidence_by_key.get(node_key, ())

    def dependencies(self) -> tuple[BaseModel, ...]:
        """Всё evidence, произведённое ВСЕМИ requires этого узла, одним
        плоским кортежем — самый частый способ, которым атому нужны его
        входы: «дай мне всё, что уже посчитано ниже», не по одному ключу."""
        out: list[BaseModel] = []
        for key in self.requires:
            out.extend(self._evidence_by_key.get(key, ()))
        return tuple(out)


def _sandbox_profile_for(atom: AtomDef) -> SandboxProfile:
    # Три фиксированных профиля — первая очередь; `DYNAMIC_SANDBOX_PROFILES`
    # (атомы внешнего обогащения, `external_enrichment(...)`, Срез 5a) — вторая,
    # НЕ смешивается с фиксированной тройкой (`test_registry_has_the_three_
    # fixed_profiles`).
    profile = SANDBOX_PROFILES.get(atom.sandbox_profile) or DYNAMIC_SANDBOX_PROFILES.get(atom.sandbox_profile)
    if profile is None:
        known = sorted(set(SANDBOX_PROFILES) | set(DYNAMIC_SANDBOX_PROFILES))
        raise SandboxViolation(
            f"атом {atom.name!r} объявляет неизвестный профиль песочницы {atom.sandbox_profile!r} — "
            f"известны: {', '.join(known)}"
        )
    return profile


def validate_sandbox_dispatch(atom: AtomDef) -> SandboxProfile:
    """A2.1.8: узел не запускается под профилем, которого его собственные
    требования не выдерживают. Детерминированный атом (`execution_kind=
    "deterministic"`) не имеет дела ни с сетью, ни со свободным кодом —
    решение №9 запрещает выдавать их атому по умолчанию, а «по умолчанию»
    здесь и есть детерминированный расчёт/проверка. Атом внешнего
    ввода-вывода (`execution_kind="io"`) обязан получить профиль, который
    вообще разрешает сеть. Атом со сгенерированным кодом (`execution_kind="generated"`)
    — только под профилем с `code_execution` и без сети: код видит данные `Case`."""
    profile = _sandbox_profile_for(atom)
    if atom.execution_kind == "deterministic" and (
        profile.network_access is not NetworkAccess.NONE or profile.code_execution
    ):
        raise SandboxViolation(
            f"атом {atom.name!r} детерминированный, но профиль {profile.name!r} даёт сеть и/или "
            "свободный код — детерминированный расчёт не имеет права на них по решению №9"
        )
    if atom.execution_kind == "io" and profile.network_access is NetworkAccess.NONE:
        raise SandboxViolation(
            f"атом {atom.name!r} объявлен как ввод-вывод (execution_kind='io'), но профиль "
            f"{profile.name!r} не даёт сети вовсе"
        )
    if atom.execution_kind == "generated" and (
        not profile.code_execution or profile.network_access is not NetworkAccess.NONE
    ):
        raise SandboxViolation(
            f"атом {atom.name!r} исполняет сгенерированный код: профиль {profile.name!r} обязан "
            "разрешать исполнение кода и не давать сети (комната станции)"
        )
    return profile


def _evidence_ids(values: Sequence[object]) -> list[str]:
    return list(dict.fromkeys(
        evidence_id for value in values if (evidence_id := result_id_of(value))
    ))


def _leaves_by_node_key(available: AvailableIndex) -> dict[NodeKey, AvailableLeaf]:
    """Листья `Case`, сгруппированные по `node_key` производителя.

    Один узел пишет в `Case` артефакты РАЗНЫХ типов под одним `node_key` (гейт
    `verify.bounds`: метрика и находка), и `AvailableIndex` хранит их отдельными листьями.
    Прежний `{leaf.node_key: leaf}` оставлял в словаре последний лист, и доказательство
    узла становилось доказательством одного типа: на живом отчёте у 84 из 92 гейтов
    метрика пропадала, а `EvidenceBundleV2` собирался из 8 фактов вместо 84. Порядок
    решал, повезёт ли: в подставных тестах он совпадал удачно. Доказательство узла —
    объединение всех его листьев (без повторов по идентификатору доказательства)."""
    merged: dict[NodeKey, AvailableLeaf] = {}
    for source_leaf in available.all_leaves():
        leaf = AvailableLeaf(
            node_key=source_leaf.node_key,
            evidence_ids=tuple(dict.fromkeys((*source_leaf.evidence_ids, *_evidence_ids(source_leaf.evidence)))),
            evidence=source_leaf.evidence,
        )
        known = merged.get(leaf.node_key)
        if known is None:
            merged[leaf.node_key] = leaf
            continue
        have = set(_evidence_ids(known.evidence))
        merged[leaf.node_key] = AvailableLeaf(
            node_key=leaf.node_key,
            evidence_ids=tuple(dict.fromkeys((*known.evidence_ids, *leaf.evidence_ids))),
            evidence=(*known.evidence, *(item for item in leaf.evidence if result_id_of(item) not in have)),
        )
    return merged


@dataclass
class ExecutionResult:
    node_runs: dict[NodeKey, NodeRun]
    evidence_by_key: dict[NodeKey, tuple[BaseModel, ...]]
    root_status: str

    @property
    def succeeded(self) -> bool:
        return self.root_status == "succeeded"

    @property
    def errors(self) -> tuple[NodeRun, ...]:
        """Узлы в терминальном сбое — единственный признак «ошибка исполнения» (часть 14, К14.1).

        «Данных нет» им не выражается: пустой результат — это `succeeded` с нулём evidence. Признак
        читают и маршрутизатор (вход в цикл, текст отказа), и описание хода для планировщика."""
        return tuple(run for run in self.node_runs.values() if run.status in TERMINAL_FAILURE)


def _batch_key(atom: AtomDef, args: NodeArgs) -> tuple:
    return (atom.name,) + tuple(node_arg_value(args, f) for f in atom.batch_key_fields)


class GraphExecutor:
    """Исполняет `PlanGraph`, узел за узлом (пакет за пакетом), от доступных
    листьев к корню — направление, обратное построению (02, §3)."""

    def __init__(self, *, registry: AtomRegistry, budget: ExecutionBudget | None = None):
        self.registry = registry
        self.budget = budget or ExecutionBudget()

    def run(
        self, graph: PlanGraph, node_args: Mapping[NodeKey, NodeArgs], *, run_id: str,
        available: AvailableIndex | None = None, run_passport: "_RunPassport | None" = None,
        inputs: RunInputs | None = None,
        prior_node_runs: Mapping[NodeKey, NodeRun] | None = None,
        prior_evidence: Mapping[NodeKey, Sequence[BaseModel]] | None = None,
        on_node_update: Callable[[NodeRun], None] | None = None,
        on_node_result: Callable[[NodeRun, tuple[BaseModel, ...]], None] | None = None,
    ) -> ExecutionResult:
        inputs = inputs or RunInputs()
        available = available or AvailableIndex()
        nodes_by_key = {n.node_key: n for n in graph.nodes}
        leaf_by_key = _leaves_by_node_key(available)
        prior_node_runs = prior_node_runs or {}
        prior_evidence = prior_evidence or {}

        node_runs: dict[NodeKey, NodeRun] = {}
        evidence: dict[NodeKey, tuple[BaseModel, ...]] = {}

        def _publish(key: NodeKey, run: NodeRun) -> None:
            node_runs[key] = run
            if on_node_update is not None:
                on_node_update(run)

        pending: list[NodeKey] = []
        for key in nodes_by_key:
            # A2.1.6/A2.1.2 — идемпотентный повтор: узел, уже успешно
            # завершённый в ПРЕДЫДУЩЕЙ попытке того же `PlanRun`, не исполняется
            # заново — ни второй раз не вызывает `executor`, ни не плодит
            # второе доказательство.
            prior = prior_node_runs.get(key)
            if prior is not None and prior.status == "succeeded":
                _publish(key, prior)
                evidence[key] = tuple(prior_evidence.get(key, ()))
                if on_node_result is not None:
                    on_node_result(prior, evidence[key])
                continue
            leaf = leaf_by_key.get(key)
            if leaf is not None:
                # Case-leaf may retain the REAL producer NodeKey. Presence in
                # AvailableIndex is what makes it existing input; atom registry
                # membership must not force re-execution of the producer.
                now = datetime.now(timezone.utc)
                _publish(key, NodeRun(
                    node_key=key, run_id=run_id, status="succeeded", sandbox_profile="compute_no_network",
                    produced_evidence_ids=list(leaf.evidence_ids),
                    started_at=now, finished_at=now,
                ))
                evidence[key] = tuple(leaf.evidence)
                if on_node_result is not None:
                    on_node_result(node_runs[key], evidence[key])
                continue
            atom = self.registry.get(key.atom_name)
            if atom is None:
                now = datetime.now(timezone.utc)
                _publish(key, NodeRun(
                    node_key=key, run_id=run_id, status="succeeded", sandbox_profile="compute_no_network",
                    produced_evidence_ids=[], started_at=now, finished_at=now,
                ))
                evidence[key] = ()
                if on_node_result is not None:
                    on_node_result(node_runs[key], evidence[key])
                continue
            prior_attempt = prior.attempt if prior is not None else 0
            _publish(key, NodeRun(
                node_key=key, run_id=run_id, status="planned", sandbox_profile=atom.sandbox_profile,
                attempt=prior_attempt + 1,
            ))
            pending.append(key)

        remaining_calls = self.budget.max_atom_calls
        started_wall = time.monotonic()

        while pending:
            if time.monotonic() - started_wall > self.budget.max_wall_seconds:
                raise TimeoutError(f"GraphExecutor: превышен бюджет времени {self.budget.max_wall_seconds}s")

            ready: list[NodeKey] = []
            blocked: list[NodeKey] = []
            still_pending: list[NodeKey] = []
            for key in pending:
                atom = self.registry.get(key.atom_name)
                if atom is not None and atom.needs_run_passport and run_passport is None:
                    # A1.5, граница двухпроходного раскрытия: узел, чей
                    # `requires` сам зависит от `RunPassport`, в графе ПЕРВОГО
                    # прохода несёт только заглушечную ссылку на identify.
                    # classify (см. `GraphPlanner._expand_atom`) — исполнить
                    # его СЕЙЧАС значило бы посчитать его по неполному
                    # набору входов и зафиксировать это как `succeeded`
                    # (ложный успех, который второй проход из-за идемпотентного
                    # возобновления, A2.1.6, принял бы за готовый результат и
                    # никогда не пересчитал). Узел остаётся `planned` — не
                    # `blocked` (это не ошибка), просто ещё не время.
                    continue
                dep_statuses = [node_runs[r].status for r in nodes_by_key[key].requires]
                if all(s == "succeeded" for s in dep_statuses):
                    ready.append(key)
                elif any(s in TERMINAL_FAILURE for s in dep_statuses):
                    blocked.append(key)
                else:
                    still_pending.append(key)

            if not ready and not blocked:
                # На ациклическом графе это означает недостижимую (по данным
                # исполнителя) зависимость вне графа — граф не прошёл
                # `GraphPlanner`, вызывающий нарушил предусловие метода.
                stuck = ", ".join(k.atom_name for k in still_pending)
                raise RuntimeError(
                    f"GraphExecutor: узлы [{stuck}] не могут стать ready — граф не был подтверждён "
                    "достижимым перед исполнением (нарушено предусловие GraphExecutor.run)"
                )

            now = datetime.now(timezone.utc)
            for key in blocked:
                _publish(key, node_runs[key].model_copy(update={
                    "status": "blocked", "started_at": now, "finished_at": now,
                    "error": "обязательная зависимость завершилась неуспешно",
                }))
                evidence[key] = ()
                if on_node_result is not None:
                    on_node_result(node_runs[key], evidence[key])

            groups = self._group_ready(ready, node_args)
            if len(groups) > remaining_calls:
                raise RuntimeError(f"GraphExecutor: превышен бюджет вызовов атомов {self.budget.max_atom_calls}")
            remaining_calls -= len(groups)

            def run_group(group) -> None:
                batch_keys, atom = group
                # Объединение requires ВСЕХ узлов пакета, не только первого.
                batch_requires = tuple(dict.fromkeys(
                    key for bk in batch_keys for key in nodes_by_key[bk].requires
                ))
                self._run_batch(
                    batch_keys, atom, node_args=node_args, node_runs=node_runs, evidence=evidence,
                    run_id=run_id, run_passport=run_passport, inputs=inputs, publish=_publish,
                    requires=batch_requires,
                )
                if on_node_result is not None:
                    for key in batch_keys:
                        on_node_result(node_runs[key], evidence.get(key, ()))

            # Only a compound DAG opts into cross-scenario concurrency. Normal
            # single-goal execution preserves its established ordering; within
            # a compound plan, all currently-ready physical batches are
            # independent by construction and can run at the same time.
            if graph.scenarios and len(groups) > 1:
                # Поток пула не наследует контекст хода (тумблер «Поиск в интернете», бюджет и трасса
                # вызовов модели): без копии атом части цели видел бы значения по умолчанию.
                contexts = [contextvars.copy_context() for _ in groups]
                with ThreadPoolExecutor(max_workers=min(len(groups), 8), thread_name_prefix="aifa-goal") as pool:
                    list(pool.map(contextvars.Context.run, contexts, repeat(run_group), groups))
            else:
                for group in groups:
                    run_group(group)

            pending = still_pending

        root_run = node_runs.get(graph.root)
        return ExecutionResult(
            node_runs=node_runs, evidence_by_key=evidence,
            root_status=root_run.status if root_run is not None else "blocked",
        )

    # ------------------------------------------------------------------
    def _group_ready(
        self, ready: Sequence[NodeKey], node_args: Mapping[NodeKey, NodeArgs],
    ) -> list[tuple[list[NodeKey], AtomDef]]:
        """A2: «Однотипные готовые узлы объединяются по `BatchKey`». Атомы без
        политики пакетирования (`batch_key_fields` пуст) получают уникальный
        ключ на узел — каждый исполняется физически отдельно, как раньше."""
        groups: dict[tuple, list[NodeKey]] = {}
        order: list[tuple] = []
        atom_of: dict[tuple, AtomDef] = {}
        for key in ready:
            atom = self.registry.get(key.atom_name)
            if atom is None:
                raise RuntimeError(f"GraphExecutor: узел {key.atom_name!r} не найден в AtomRegistry")
            group_key = _batch_key(atom, node_args[key]) if atom.is_batchable else (atom.name, key)
            if group_key not in groups:
                groups[group_key] = []
                atom_of[group_key] = atom
                order.append(group_key)
            groups[group_key].append(key)
        return [(groups[gk], atom_of[gk]) for gk in order]

    def _run_batch(
        self, keys: list[NodeKey], atom: AtomDef, *, node_args: Mapping[NodeKey, NodeArgs],
        node_runs: dict[NodeKey, NodeRun], evidence: dict[NodeKey, tuple[BaseModel, ...]],
        run_id: str, run_passport: "_RunPassport | None", inputs: RunInputs,
        publish: Callable[[NodeKey, NodeRun], None], requires: tuple[NodeKey, ...],
    ) -> None:
        try:
            validate_sandbox_dispatch(atom)
        except SandboxViolation as exc:
            now = datetime.now(timezone.utc)
            for key in keys:
                publish(key, node_runs[key].model_copy(update={
                    "status": "blocked", "started_at": now, "finished_at": now, "error": str(exc),
                }))
                evidence[key] = ()
            return

        now = datetime.now(timezone.utc)
        for key in keys:
            publish(key, node_runs[key].model_copy(update={"status": "running", "started_at": now}))

        ctx = ExecutionContext(
            run_id=run_id, run_passport=run_passport, inputs=inputs, evidence_by_key=evidence, requires=requires,
        )
        max_retries = self.budget.max_retries if atom.execution_kind in ("model", "io") else 0

        # Вызов через batch-соглашение (список in, словарь out) идёт по
        # `atom.is_batchable`, а не по фактическому размеру пакета: если бы
        # единственный готовый узел пакетируемого атома вызывался НЕ-batch
        # сигнатурой, исполнитель атома получал бы разные формы вызова в
        # зависимости от того, сколько узлов оказалось готово одновременно —
        # то есть от порядка исполнения, а не от контракта атома.
        if atom.is_batchable:
            self._run_batched_call(keys, atom, ctx=ctx, node_args=node_args,
                                    node_runs=node_runs, evidence=evidence, publish=publish, max_retries=max_retries)
        else:
            for key in keys:
                self._run_single(key, atom, ctx=ctx, node_args=node_args[key],
                                  node_runs=node_runs, evidence=evidence, publish=publish, max_retries=max_retries)

    def _run_single(
        self, key: NodeKey, atom: AtomDef, *, ctx: ExecutionContext, node_args: NodeArgs,
        node_runs: dict[NodeKey, NodeRun], evidence: dict[NodeKey, tuple[BaseModel, ...]],
        publish: Callable[[NodeKey, NodeRun], None], max_retries: int,
    ) -> None:
        attempt = node_runs[key].attempt
        retries = 0
        while True:
            try:
                produced = tuple(atom.executor(node_args, ctx) or ())
            except Exception as exc:  # noqa: BLE001 — граница атома: любая его ошибка становится статусом узла, не падением исполнителя
                if retries < max_retries:
                    retries += 1
                    attempt += 1
                    continue
                finished = datetime.now(timezone.utc)
                publish(key, node_runs[key].model_copy(update={
                    "status": "failed", "error": str(exc), "finished_at": finished, "attempt": attempt,
                }))
                evidence[key] = ()
                return
            finished = datetime.now(timezone.utc)
            evidence[key] = produced
            publish(key, node_runs[key].model_copy(update={
                "status": "succeeded", "finished_at": finished, "attempt": attempt,
                "produced_evidence_ids": _evidence_ids(produced),
            }))
            return

    def _run_batched_call(
        self, keys: list[NodeKey], atom: AtomDef, *, ctx: ExecutionContext, node_args: Mapping[NodeKey, NodeArgs],
        node_runs: dict[NodeKey, NodeRun], evidence: dict[NodeKey, tuple[BaseModel, ...]],
        publish: Callable[[NodeKey, NodeRun], None], max_retries: int,
    ) -> None:
        base_attempts = {key: node_runs[key].attempt for key in keys}
        retries = 0
        args_list = [node_args[k] for k in keys]
        while True:
            try:
                results = atom.executor(args_list, ctx) or {}
            except Exception as exc:  # noqa: BLE001 — сбой ВСЕГО физического вызова (не одного концепта)
                if retries < max_retries:
                    retries += 1
                    continue
                finished = datetime.now(timezone.utc)
                for key in keys:
                    publish(key, node_runs[key].model_copy(update={
                        "status": "failed", "error": str(exc), "finished_at": finished,
                        "attempt": base_attempts[key] + retries,
                    }))
                    evidence[key] = ()
                return
            break

        finished = datetime.now(timezone.utc)
        for key in keys:
            concept = node_args[key].concept
            produced = results.get(concept) if concept else None
            if produced is None:
                # A2.1.4: ошибка/отсутствие ОДНОГО концепта в пакете не
                # переводит успешно извлечённых соседей в ошибку — только этот узел.
                publish(key, node_runs[key].model_copy(update={
                    "status": "failed", "error": f"пакетный вызов {atom.name!r} не вернул {concept!r}",
                    "finished_at": finished, "attempt": base_attempts[key] + retries,
                }))
                evidence[key] = ()
                continue
            produced = tuple(produced)
            evidence[key] = produced
            publish(key, node_runs[key].model_copy(update={
                "status": "succeeded", "finished_at": finished,
                "attempt": base_attempts[key] + retries,
                "produced_evidence_ids": _evidence_ids(produced),
            }))


__all__ = [
    "TERMINAL_FAILURE",
    "SandboxViolation",
    "RunInputs",
    "ExecutionBudget",
    "ExecutionContext",
    "ExecutionResult",
    "GraphExecutor",
    "validate_sandbox_dispatch",
]
