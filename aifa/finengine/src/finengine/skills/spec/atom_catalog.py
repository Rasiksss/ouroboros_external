"""Каталог атомов для цикла гибридного планировщика.

Когда детерминированный `GraphPlanner` не смог собрать план, к делу подключается
`DynamicRun` с инструкцией `dynamic_run_planner.md`. Ему нужно КРАТКОЕ описание
всех атомов: что делает, что даёт, что требует. Часть каталога выводится из самого
реестра (`produces`/`requires`/гейты) — оно не может разойтись с кодом; человеческое
описание — в `ATOM_DESCRIPTIONS`, и тест-страж `tests/planner/test_atom_catalog.py`
следит, чтобы оно покрывало ровно реестр.

Гейты (`norm.*`, `verify.*`) и инфраструктурные атомы (`ingest.document`,
`identify.classify`) агенту НЕ предлагаются как шаги: планировщик достраивает их сам
по `mandatory_gates`/`requires` — агент, которому дали такую возможность, мог бы
обойти обязательную проверку, просто не включив её в план.
"""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry

ATOM_DESCRIPTIONS: dict[str, str] = {
    "goal.join": "Структурный fan-in составного запроса; ждёт корни сценариев и не создаёт финансовых данных.",
    "ingest.document": "Принимает текст загруженного документа как вход прогона (без сети).",
    "identify.classify": "Один раз на запуск определяет профиль отчётности (стандарт, тип компании, язык) и фиксирует RunPassport.",
    "doc.textify": "Постраничный текст документа (при необходимости OCR).",
    "doc.index": "Индекс документа: страницы, таблицы и их подписи.",
    "locate": "Стадия 1 двухстадийного извлечения: по названию и синонимам показателя находит страницы и таблицы отчёта. Один вызов на пакет показателей.",
    "read.value": "Стадия 2: читает значение показателя (за все периоды отчёта) из найденных мест. Концепт — код реестра или ad-hoc понятие; проверки единиц, времени и границ идут следом автоматически.",
    "read.structure": "Извлекает структурированную таблицу раскрытия; сейчас поддержаны сегменты (concept=\"segments\").",
    "read.quote": "Дословная цитата из документа по теме или вопросу (concept = тема); привязка цитаты к странице проверяется автоматически.",
    "read.entities": "Состав группы и связи между компаниями: кто кем владеет и в какой доле.",
    "read.override": "Явная поправка пользователя к ранее извлечённому значению.",
    "calc.formula": "Детерминированный расчёт показателя по формуле реестра или ad-hoc формуле из проверенных входов; входы формулы планировщик раскрывает сам.",
    "verify.arithmetic": "Проверка арифметики рассчитанного факта.",
    "norm.unit": "Гейт: приведение единиц измерения извлечённого значения.",
    "norm.time": "Гейт: согласованность периода (дата или интервал), трассировка годизации.",
    "verify.bounds": "Гейт: значение находится в правдоподобных границах реестра.",
    "verify.identity": "Гейт: сверка расчёта с извлечённым значением по правилу тождества реестра.",
    "verify.cross": "Гейт: перекрёстная сверка двух независимых способов получить показатель.",
    "verify.citation": "Гейт: цитата действительно найдена на указанной странице документа.",
    "analyze.full_bundle": "Пакет доказательств полного анализа: показатели и структуры, состав которых определяет роль пользователя (или список requested_concepts).",
    "out.form": "Форма роли: таблица показателей по раскладке роли или по списку requested_concepts; недоступные строки сохраняются.",
    "ref.prior": "Значение того же показателя за предыдущий период — точка сравнения.",
    "assess.judgment": "Суждение модели по методике о показателе на основе уже извлечённых доказательств.",
    "explain.internal": "Объяснение показателя по данным самого документа.",
    "explain.external": "Новости о самой компании и решения её регуляторов за период отчёта. Запросы строит система, тему задать нельзя. Любой другой внешний вопрос — web.search.",
    "web.search": "Поиск в интернете. concept — текст запроса (обязателен); extra.topic — general (справочные страницы, без дат) или news (новости с датой публикации). Даёт находки: заголовок, адрес, фрагмент, дата. Не источник чисел отчётности. Страницу по адресу находки скачивает атом-загрузка.",
    "out.narrative": "Аналитический текст (вывод или объяснение изменения) по суждениям и объяснениям выбранных показателей (requested_concepts); числа проверяются на опору в доказательствах.",
    "out.changes": "Изменения показателей между периодами одного отчёта: таблица (Δ и Δ %, считает код) плюс объяснение; корень для MONITOR_CHANGES.",
    "out.report": "Итоговый отчёт: структурирует аналитический текст и доказательства; проверки опоры и цитат обязательны.",
    "out.file": "Файл xlsx, pdf, csv или json из формы, отчёта или аналитического текста.",
    "verify.grounding": "Гейт: числовые утверждения текста опираются на доказательства.",
    "id.revision": "Определяет, является ли документ пересмотренной или переизданной версией.",
    "set.assemble": "Собирает набор документов одной сущности по периоду и типу.",
    "read.condition": "Условие договора или ковенанта из документа (с проверкой цитаты).",
    "ref.plan": "Плановое значение показателя как точка сравнения.",
    "ref.norm": "Нормативное или пороговое значение показателя как точка сравнения.",
    "assess.rule": "Сравнение факта с планом, нормой или порогом по правилу.",
    "calc.series": "Динамика показателей между двумя снимками одного субъекта.",
    "calc.decompose": "Разложение изменения показателя на факторы.",
    "ref.peer": "Референсный набор аналогов для сопоставления.",
    "watch.subscribe": "Подписка на наблюдение за изменениями субъекта.",
    "watch.diff": "Различия между двумя снимками одного субъекта.",
    "watch.approach": "Приближение показателей к порогам между двумя снимками.",
    "monitor.compose": "Итог наблюдения: различия, динамика и приближения к порогам в одном результате.",
}

# Не предлагаются агенту как шаги: гейты и инфраструктура достраиваются
# планировщиком сами; `read.override` — отдельный путь пользовательских поправок
# с аудитом; `watch.subscribe` — побочный эффект (подписка), не «ответ на запрос».
NOT_PLANNABLE: frozenset[str] = frozenset({
    "goal.join", "ingest.document", "identify.classify", "doc.textify", "doc.index",
    "norm.unit", "norm.time", "verify.bounds", "verify.identity", "verify.cross",
    "verify.citation", "verify.grounding", "verify.arithmetic",
    "read.override", "watch.subscribe",
})

_MANY_CONCEPTS = 8


def plannable_atoms(registry: AtomRegistry) -> tuple[AtomDef, ...]:
    return tuple(atom for atom in registry.all() if atom.name not in NOT_PLANNABLE)


def _produces_summary(atom: AtomDef) -> str:
    by_type: dict[str, list[str | None]] = {}
    for spec in atom.produces:
        by_type.setdefault(spec.output_type, []).append(spec.concept)
    parts: list[str] = []
    for output_type, concepts in by_type.items():
        named = [c for c in concepts if c]
        if len(named) > _MANY_CONCEPTS or (named and None in concepts):
            parts.append(f"{output_type}[любой показатель реестра или ad-hoc]")
        elif named:
            parts.append(", ".join(f"{output_type}[{c}]" for c in named))
        else:
            parts.append(output_type)
    return ", ".join(parts)


def _requires_summary(atom: AtomDef) -> str:
    parts = [
        spec.input_type + (f"[{spec.concept}]" if spec.concept else "") for spec in atom.requires
    ]
    if atom.dynamic_requires is not None:
        parts.append("входы по концепту (раскрываются автоматически)")
    if atom.collection_scope or atom.dynamic_collection_scope is not None:
        parts.append("коллекция по роли или requested_concepts")
    if atom.needs_run_passport:
        parts.append("RunPassport")
    return "; ".join(parts) or "—"


def catalog_text(registry: AtomRegistry) -> str:
    """Компактный каталог: по строке на атом — описание, что даёт, что требует."""
    lines = [
        "Каталог атомов (шаг плана = вызов ОДНОГО атома). Гейты проверок, чтение документа, "
        "классификация и входы по типам достраиваются автоматически — их в шаги не включай.",
    ]
    for atom in plannable_atoms(registry):
        line = (
            f"- {atom.name} ({atom.execution_kind}, {atom.cost_class}): "
            f"{ATOM_DESCRIPTIONS.get(atom.name, '')} "
            f"Даёт: {_produces_summary(atom)}. Требует: {_requires_summary(atom)}."
        )
        if atom.mandatory_gates:
            line += f" Гейты (автоматически): {', '.join(atom.mandatory_gates)}."
        lines.append(line)
    return "\n".join(lines)


__all__ = ["ATOM_DESCRIPTIONS", "NOT_PLANNABLE", "catalog_text", "plannable_atoms"]
