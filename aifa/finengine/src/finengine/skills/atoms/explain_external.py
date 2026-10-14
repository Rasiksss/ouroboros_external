"""`explain.external` — карточка 16b: связать проверенные внутренние
изменения с внешними фактами, маршрутизируя по сущности/юрисдикции/периоду, а
не по языку документа.

**Юрисдикция.** `finengine.skills.analysis.external.jurisdiction_of` читает
`RunPassport` — сигналы, уже зафиксированные решением №2 (регистрационные
номера эмитента, стандарт, ячейка профиля), не язык текста, пересчитанный
заново (см. комментарий в том модуле). Юрисдикция не определена — поиск идёт
БЕЗ регуляторного среза (только запрос по компании), а не с угаданной страной.

**Тема не задаётся.** Атом собирает новости о компании и решения её
регуляторов за период; концепт шага он не читает. Произвольный внешний вопрос
— атом `web.search` (часть 19 доводки).

**Внешний факт не подменяет число из отчётности** (нерушимое решение №3) —
`ExternalFactRef` не участвует ни в одном численном сравнении, только в
качественном объяснении, и того же принципа держится `explain.internal`
(разные атомы, не пересекаются на уровне `Target.output_type`).

**Сеть.** Реальный сбор идёт через уже существующий `analysis.external.
collect` (тумблер пользователя/наличие поиска — та же единственная дверь,
`user_allowed_web_search`/`_ws.is_enabled`). `collector` внедряем параметром
ради тестируемости без сети — тот же приём, что `locate_executor`/
`read_value_executor` в `graph_atoms.build_registry`."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from finengine.contracts import ExternalFactRef, RunPassport
from finengine.skills.analysis import external as _ext
from finengine.skills.spec.atom_registry import NodeArgs

ATOM_VERSION = "1.0.0"

Collector = Callable[..., Awaitable[list]]


def _default_collector(**kwargs) -> Awaitable[list]:
    return _ext.collect(**kwargs)


def explain_external_executor(
    args: NodeArgs, ctx, *, collector: Collector | None = None,
) -> list[ExternalFactRef]:
    passport: RunPassport | None = ctx.run_passport
    if passport is None:
        raise ValueError("explain.external: RunPassport не зафиксирован — узел не должен был стать ready")

    jurisdiction = _ext.jurisdiction_of(passport)
    period_ref = args.period_ref or ""
    collect_fn = collector or _default_collector
    facts = asyncio.run(collect_fn(
        company_name=passport.entity.canonical_name, period=period_ref,
        language=passport.classification.language, jurisdiction=jurisdiction, require_name_match=True,
    ))

    refs: list[ExternalFactRef] = []
    for fact in facts:
        if not jurisdiction:
            # Юрисдикция не определена по стандарту — факт остаётся вне
            # доверенного объяснения (карточка: «Каждый внешний факт... имеет...
            # связь с сущностью/юрисдикцией»), не подставляем угадку.
            continue
        refs.append(ExternalFactRef(
            text=fact.text, entity_id=passport.entity.entity_id, jurisdiction=jurisdiction,
            date=fact.date, source=fact.source, url=fact.url, period_ref=period_ref or None,
        ))
    return refs


__all__ = ["ATOM_VERSION", "explain_external_executor", "Collector"]
