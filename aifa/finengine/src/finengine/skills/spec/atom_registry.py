"""`AtomRegistry` — реестр СПОСОБНОСТЕЙ, не пользовательских сценариев (A1.1,
docs/build-plan/13-srez-03-graph-planner-executor.md).

`AtomDef` — данные, не поведение: `@dataclass(frozen=True)`, тот же выбор, что
уже сделан в репозитории для `Step` (`skills/spec/steps.py`) и `RolePlan`
(`roles/scope.py`) — контракты pydantic (`finengine.contracts`) остаются для
доменных данных, которые сериализуются и путешествуют между слоями; здесь же
живёт исполняемая ссылка (`executor`) и питоновские callable'ы, которым
`pydantic.Contract` (`frozen=True, extra="forbid"`) только мешал бы.

`GraphPlanner` (`graph_planner.py`, этот же срез) — единственный потребитель
этого реестра, который решает, ЧТО раскрывать. Сам реестр не знает про корневые
вершины, `TargetNode` или конкретные запросы пользователя — только про то, что
каждый атом умеет потребовать (`requires`) и произвести (`produces`)."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from finengine.skills.spec.target import METRIC_EVIDENCE_MEMBERS, InputSpec, OutputSpec

if TYPE_CHECKING:
    from finengine.contracts import RunPassport

# Зарезервированное имя концепта: атом, объявивший его в `produces`, — это
# производитель `RunPassport` для двухпроходного раскрытия (A1.5). Планировщик
# ищет производителя по этому имени механически, а не по имени атома
# `identify.classify` — так правило остаётся данными реестра, а не веткой если
# в коде планировщика на конкретное имя атома.
RUN_PASSPORT_CONCEPT = "RunPassport"

# Поля идентичности узла, которые `fingerprint()` умеет прочитать напрямую из
# `NodeArgs` (A1.2: «атом, вид цели, концепт, версию документа, период, базис,
# версию политики заказчика»). Атом-специфичные дополнительные поля (роль,
# id формулы...) идут через `NodeArgs.extra` и требуют совпадения имени поля,
# указанного в `AtomDef.key_fields`, с ключом в `extra`.
_CORE_FIELD_LABELS: dict[str, str] = {
    "concept": "concept",
    "entity_ref": "entity",
    "period_ref": "period",
    "basis": "basis",
    "document_version": "doc_version",
    "policy_version": "policy_version",
}


@dataclass(frozen=True)
class NodeArgs:
    """Значимые входы одного узла — то, из чего строится `NodeKey.args_fingerprint`
    и что передаётся исполнителю атома. Не сам `NodeKey` (он несёт уже готовую
    строку-отпечаток и имя/версию атома) — `NodeArgs` живёт только во время
    построения графа и вызова исполнителя."""

    concept: str | None = None
    entity_ref: str | None = None
    period_ref: str | None = None
    basis: str | None = None
    document_version: str | None = None
    policy_version: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)


def node_arg_value(args: NodeArgs, field_name: str) -> str | None:
    """Значение одного поля `NodeArgs` по имени — ядрового (`concept`,
    `entity_ref`, ...) или атом-специфичного (через `extra`). Общая точка
    чтения для `fingerprint()` (планировщик) и группировки в `BatchKey`
    (`graph_executor.py`, A2) — обе обязаны понимать имена полей одинаково."""
    if field_name in _CORE_FIELD_LABELS:
        return getattr(args, field_name)
    return args.extra.get(field_name)


def _norm_output_specs(items: Sequence["str | OutputSpec"]) -> tuple[OutputSpec, ...]:
    """Обратная совместимость (retrofit v9, R3): голая строка в `produces` —
    то же самое, что `OutputSpec(output_type=строка)` (артефакт без измерения
    "какой концепт" — `RunPassport`, `document_text`, `EvidenceBundleV2`, ...).
    Тесты/старые вызывающие продолжают писать `produces=("REVENUE",)`, не
    зная про `OutputSpec`, там, где различение по концепту не нужно."""
    return tuple(item if isinstance(item, OutputSpec) else OutputSpec(output_type=item) for item in items)


def _norm_input_specs(items: Sequence["str | InputSpec"]) -> tuple[InputSpec, ...]:
    return tuple(item if isinstance(item, InputSpec) else InputSpec(input_type=item) for item in items)


def fingerprint(args: NodeArgs, key_fields: Sequence[str]) -> str:
    """Детерминированная строка `имя=значение;имя=значение` по ПОРЯДКУ,
    объявленному атомом в `AtomDef.key_fields` — не отсортированная по
    алфавиту: порядок полей для одного и того же атома всегда один и тот же
    (сам атом его объявляет один раз), а для человека, читающего граф,
    «concept;period;entity» читабельнее «basis;concept;doc_version;entity».

    Пустые/отсутствующие поля пропускаются, а не пишутся как `field=`: узел
    без периода (например `ingest.document` без применимого периода) не
    должен получать отпечаток с висящим пустым значением.
    """
    parts: list[str] = []
    for name in key_fields:
        value = node_arg_value(args, name)
        if value:
            parts.append(f"{_CORE_FIELD_LABELS.get(name, name)}={value}")
    if not parts:
        raise ValueError(
            f"fingerprint(): ни одно из key_fields={key_fields!r} не дало значения — "
            "атом с пустой идентичностью не может дедуплицироваться (A1.2)"
        )
    return ";".join(parts)


# Исполнитель атома. Небатчируемый: (NodeArgs, ExecutionContext) -> list[Contract].
# Батчируемый (см. `AtomDef.batch_key_fields`): (list[NodeArgs], ExecutionContext)
# -> dict[str, list[Contract]], ключ — `NodeArgs.concept` внутри одной группы
# `BatchKey` (A2: «результат раскладывается обратно по логическим узлам»).
# `ExecutionContext` объявлен в `graph_executor.py` (этот же срез) — типизирован
# здесь как `Any`, чтобы не заводить цикл импорта реестр↔исполнитель ради
# одной аннотации.
AtomExecutor = Callable[..., Any]
# Retrofit v9 (R3): возвращает `InputSpec` (голые строки по-прежнему приняты —
# `graph_planner.py` нормализует результат тем же `_norm_input_specs`, что
# `AtomDef.__post_init__` применяет к статическому `requires`).
DynamicRequires = Callable[[NodeArgs, "RunPassport | None"], "tuple[InputSpec | str, ...] | None"]
RequirementExtra = Callable[[NodeArgs, InputSpec], Mapping[str, str]]
# Проверка аргументов шага, названного планировщиком цикла: текст проблемы либо `None`.
StepCheck = Callable[[NodeArgs], "str | None"]


@dataclass(frozen=True)
class AtomDef:
    """Одна запись реестра (A1.1): «имя, версия, requires, produces,
    обязательные контрольные этапы, ссылка на исполнитель, тип исполнения,
    профиль песочницы, политика пакетирования, политика кэша, класс
    стоимости» — все перечисленные в карточке поля присутствуют здесь по
    имени, ни одно не подразумевается неявно.

    Retrofit v9 (R3): `produces`/`requires` типизированы через `OutputSpec`/
    `InputSpec` (`target.py`) — различают РОД артефакта (`output_type`), не
    только имя концепта, так что `ExtractedMetric(EBITDA)` и
    `CalculatedMetric(EBITDA)` разные `OutputSpec`, не один и тот же голый
    `"EBITDA"`. Голая строка на входе конструктора по-прежнему принимается и
    нормализуется в `OutputSpec(output_type=строка)`/`InputSpec(input_type=
    строка)` — обратная совместимость для вызывающих, которым различение по
    концепту не нужно (см. `_norm_output_specs`/`_norm_input_specs`)."""

    name: str
    version: str
    produces: tuple[OutputSpec, ...]
    sandbox_profile: str
    execution_kind: str  # "deterministic" | "model" | "io" | "generated" (код, написанный моделью)
    cache_policy: str  # "run_scoped" | "none" — полноценное кэширование через `Case` — Срез 10, здесь только заявленная политика
    cost_class: str  # "free" | "cheap" | "model_call"
    executor: AtomExecutor
    requires: tuple[InputSpec, ...] = ()
    # Retrofit v9 (R3, A1.7): БЫЛО `optional_requires`/`dynamic_optional_requires`
    # — параметр `optional=True` внутри общего разрешения `requires`, из-за
    # которого недостижимость глубже одного уровня (продюсер необязательной
    # ветки сам не может собрать СВОИ обязательные входы) всё равно портила
    # общий `state.unreachable` (см. `RETROFIT-REPORT-v9.md`, раздел «Решение
    # по optional requires»). `collection_scope`/`dynamic_collection_scope` —
    # СТРУКТУРНО отдельный путь: `GraphPlanner` раскрывает каждую запись
    # ИЗОЛИРОВАННЫМ пробным проходом (`_try_visit_isolated`) и при неудаче
    # просто не заводит эту ветвь в `requires` узла — "отсутствие ветви в
    # конкретном Target/scope" (`02-tekushee-sostoyanie-i-mekhanika.md`), не
    # nullable-ребро. Неудача идёт в `PlanBuildResult.incomplete_scope`, не в
    # `state.unreachable`/`feasibility.feasible`.
    collection_scope: tuple[InputSpec, ...] = ()
    dynamic_collection_scope: DynamicRequires | None = None
    mandatory_gates: tuple[str, ...] = ()
    batch_key_fields: tuple[str, ...] = ()  # пусто = атом не пакетируется
    key_fields: tuple[str, ...] = ("concept", "entity_ref", "period_ref")
    dynamic_requires: DynamicRequires | None = None
    # Root-specific metadata is inherited by requirements by default. Atoms
    # whose children need a narrower view project it explicitly instead of
    # teaching GraphPlanner domain-specific key names.
    requirement_extra: RequirementExtra | None = None
    needs_run_passport: bool = False
    # Что атом требует от шага цикла сверх общих правил (обязательный концепт, допустимые значения
    # `extra`): правило — данные реестра, а не ветка по имени атома в проверке шагов.
    step_check: StepCheck | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("AtomDef requires a non-empty name and version")
        if self.execution_kind not in ("deterministic", "model", "io", "generated"):
            raise ValueError(f"unknown execution_kind {self.execution_kind!r} for atom {self.name}")
        if self.cost_class not in ("free", "cheap", "model_call"):
            raise ValueError(f"unknown cost_class {self.cost_class!r} for atom {self.name}")
        object.__setattr__(self, "produces", _norm_output_specs(self.produces))
        object.__setattr__(self, "requires", _norm_input_specs(self.requires))
        object.__setattr__(self, "collection_scope", _norm_input_specs(self.collection_scope))

    @property
    def is_batchable(self) -> bool:
        return bool(self.batch_key_fields)


class AtomRegistry:
    """Таблица возможностей: имя атома → `AtomDef`, плюс обратный индекс
    concept → список атомов, объявивших его в `produces` — `GraphPlanner`
    (A1.4, шаг 4) обязан видеть ВСЕХ кандидатов сразу, а не первого попавшегося,
    чтобы правило A1.6 («нельзя молча брать первого зарегистрированного»)
    вообще имело что сравнивать."""

    def __init__(self, atoms: Sequence[AtomDef]):
        by_name: dict[str, AtomDef] = {}
        for atom in atoms:
            if atom.name in by_name:
                raise ValueError(f"дублирующееся имя атома в реестре: {atom.name!r}")
            by_name[atom.name] = atom
        self._by_name = by_name
        # Retrofit v9 (R3): ключ — пара (output_type, concept), не голый
        # концепт — `ExtractedMetric(EBITDA)` и `CalculatedMetric(EBITDA)`
        # разные записи, не два производителя одного и того же ключа.
        by_output: dict[tuple[str, str | None], list[AtomDef]] = {}
        for atom in atoms:
            for spec in atom.produces:
                by_output.setdefault((spec.output_type, spec.concept), []).append(atom)
        self._by_output = by_output

    def get(self, name: str) -> AtomDef | None:
        return self._by_name.get(name)

    def _producers_of_single(self, output_type: str, concept: str | None) -> tuple[AtomDef, ...]:
        exact = self._by_output.get((output_type, concept), ())
        if exact or concept is None:
            return tuple(exact)
        # Retrofit v9 (R4/R5a): концепт вне реестра (`AdHocConceptSpec`/
        # `AdHocFormulaSpec`) не имеет своей точной записи `(output_type,
        # concept)` — производитель, зарегистрированный с `concept=None`
        # (`read.value`/`calc.formula_engine`/`locate` несут такую запись
        # наравне со своими per-enum), обслуживает ЛЮБОЙ концепт этого
        # output_type наравне с зарегистрированными. Точное совпадение, если
        # оно есть, всегда в приоритете — catch-all это запасной путь, не
        # замена.
        return tuple(self._by_output.get((output_type, None), ()))

    def producers_of(self, output_type: str, concept: str | None = None) -> tuple[AtomDef, ...]:
        """Все зарегистрированные производители `(output_type, concept)`, в
        порядке регистрации. Порядок здесь не значит приоритет — `GraphPlanner`
        обязан решать через `PlanPolicy` (A1.6), не брать `[0]`.

        `output_type="MetricEvidence"` — не настоящий производимый тип, а
        alias-объединение (`contracts.evidence.MetricEvidence`, R2): «любое
        доказанное значение показателя», не различая род происхождения —
        нужен собственным входам формулы (`calc.formula_engine`) и
        коллекциям полного анализа/формы, которым всё равно, откуда взято
        число, лишь бы оно было доказано.

        Объединение матчит члены СТРОГО (без catch-all `_producers_of_single`
        для КАЖДОГО члена): иначе показатель, который реально ТОЛЬКО
        извлекается (например `SALES_REVENUE` — есть в `extractable`, но нет
        в `calculated`), получал бы `calc.formula_engine` вторым «кандидатом»
        через catch-all `concept=None`, хотя формулы для него не существует
        — `calc.formula_engine` тогда молча раскрывался бы с пустыми
        `dynamic_requires` (`_calc_formula_deps` не находит запись) и почти
        бесплатно ПОБЕЖДАЛ БЫ `read.value` по cost model, возвращая на
        исполнении ноль доказательств. Catch-all предназначен для ad-hoc
        концептов, запрошенных НАПРЯМУЮ через `ExtractedMetric`/
        `CalculatedMetric` (`TargetFactory`), не для каждого члена union."""
        if output_type == "MetricEvidence":
            seen: list[AtomDef] = []
            for member in METRIC_EVIDENCE_MEMBERS:
                for atom in self._by_output.get((member, concept), ()):
                    if atom not in seen:
                        seen.append(atom)
            return tuple(seen)
        return self._producers_of_single(output_type, concept)

    def all(self) -> tuple[AtomDef, ...]:
        return tuple(self._by_name.values())

    def fingerprint(self) -> str:
        """Детерминированный отпечаток формы реестра (A1.9: хэш плана
        зависит от «версий `AtomRegistry`/`PlanPolicy`»). Только КОНТРАКТ
        каждого атома (имя, версия, requires/produces/gates) — не сам
        `executor`: питоновский callable не сериализуется стабильно между
        процессами, а форма контракта — это ровно то, что меняет состав
        графа."""
        import hashlib

        shape = sorted(
            (a.name, a.version, a.requires, a.produces, a.mandatory_gates)
            for a in self._by_name.values()
        )
        return hashlib.sha256(repr(shape).encode("utf-8")).hexdigest()[:16]

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


__all__ = [
    "RUN_PASSPORT_CONCEPT",
    "NodeArgs",
    "fingerprint",
    "AtomExecutor",
    "DynamicRequires",
    "AtomDef",
    "AtomRegistry",
    "InputSpec",
    "OutputSpec",
]
