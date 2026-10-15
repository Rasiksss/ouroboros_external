"""Перенесённая схема зовёт наш пакет тем, что он принимает (#257).

ЗАЧЕМ ЭТОТ ФАЙЛ. Блокеры переноса вскрывались ПО ОДНОМУ и только прогоном:

    _coerce_period_values() got an unexpected keyword argument 'period_calendar'
    normalize_metric_period() got an unexpected keyword argument 'fiscal_context'
    _calculate_metrics_for_periods() ... 'period_calendar'
    _select_display_periods() ... 'period_calendar'

Четыре звена одной цепочки, и каждое стоило отдельного прогона: правка →
запуск → следующее падение. Причина ровно та, о которой предупреждает третье
правило измерения в SESSION_STATE: проверялось НАЛИЧИЕ имени, а не сигнатура.

Сторож делает это статически и разом. Имена вызовов берутся обходом дерева
перенесённого файла, сигнатуры — `inspect.signature` живых объектов пакета.
Ловятся оба вида обращения: прямое `foo(...)` и через псевдоним `bulk.foo(...)`
— вторых большинство, и по `ast.Name` они не видны вовсе.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `period_calendar` из сигнатуры
`_coerce_period_values` — покраснеет `test_no_call_passes_an_argument_we_reject`.

⚠️ ЧЕГО СТОРОЖ НЕ ЛОВИТ, СОЗНАТЕЛЬНО. Позиционные аргументы и типы значений:
для них нужен запуск. Он ловит один класс — именованный аргумент, которого
принимающая сторона не объявляет, — но именно он и стоил четырёх прогонов.
"""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

PORTED = (Path(__file__).resolve().parents[1]
          / "src/finengine/skills/two_stage_extractor.py")

# Псевдонимы модулей, которыми перенесённый файл зовёт наш пакет.
ALIASES = {
    "bulk": "finengine.skills.bulk_extractor",
    "llm": "finengine.llm.openrouter",
}

# Модули, которых у нас НЕТ намеренно, и импорт каждого закрыт условием.
# Пополнять только вместе с разбором, чем именно закрыт вход.
DELIBERATELY_ABSENT = {
    # показателя TOP_BORROWERS нет в наших ячейках; вход закрыт
    # `include_top_borrowers=False` и try/except ModuleNotFoundError
    "finengine.skills.top_borrowers",
    # FAISS на пути станции не исполняется (текст приезжает файлом), вход
    # закрыт условием `if index is None`; сам снос — #248
    "finengine.ingestion.indexer",
}


def _calls(tree: ast.Module):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            yield node, node.func.id, _resolve_plain(node.func.id)
        elif (isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name)
              and node.func.value.id in ALIASES):
            mod = importlib.import_module(ALIASES[node.func.value.id])
            yield (node, f"{node.func.value.id}.{node.func.attr}",
                   getattr(mod, node.func.attr, None))


def _resolve_plain(name: str):
    for target in ("finengine.skills.bulk_extractor",
                   "finengine.skills.period_validation",
                   "finengine.skills.periods"):
        mod = importlib.import_module(target)
        if hasattr(mod, name):
            return getattr(mod, name)
    return None


def test_no_call_passes_an_argument_we_reject():
    """ГЛАВНОЕ: ни один вызов не передаёт имя, которого адресат не принимает."""
    tree = ast.parse(PORTED.read_text(encoding="utf-8"))
    bad, checked = [], 0
    for node, label, obj in _calls(tree):
        if obj is None or not callable(obj):
            continue
        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError):
            continue
        checked += 1
        if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
            continue
        for kw in node.keywords:
            if kw.arg and kw.arg not in sig.parameters:
                bad.append(f"{PORTED.name}:{node.lineno} {label}({kw.arg}=...) — "
                           f"принимаем: {', '.join(sig.parameters)}")
    assert checked >= 40, (
        f"сторож ослеп: разобрано всего {checked} вызовов. Так уже было — "
        "пока учитывались только `foo(...)` и не учитывались `bulk.foo(...)`, "
        "проверка показывала 6 вызовов на файле в 2500 строк и пропускала всё")
    assert not bad, "перенесённая схема зовёт нас тем, что мы не принимаем:\n" + "\n".join(bad)


def test_every_import_resolves_or_is_a_named_exception():
    """Каждый импорт разрешается, либо назван в списке осознанных пропусков."""
    tree = ast.parse(PORTED.read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("finengine"):
            continue
        if node.module in DELIBERATELY_ABSENT:
            continue
        try:
            mod = importlib.import_module(node.module)
        except ImportError as exc:
            missing.append(f"{PORTED.name}:{node.lineno} нет модуля {node.module} — {exc}")
            continue
        for alias in node.names:
            if hasattr(mod, alias.name):
                continue
            # `from finengine.llm import openrouter` — это ПОДМОДУЛЬ, и
            # атрибутом пакета он становится только после импорта. Проверять
            # одним `hasattr` нельзя: сторож объявит пропажей рабочий импорт.
            submodule = f"{node.module}.{alias.name}"
            if submodule in DELIBERATELY_ABSENT:
                continue
            try:
                importlib.import_module(submodule)
            except ImportError:
                missing.append(
                    f"{PORTED.name}:{node.lineno} в {node.module} нет имени {alias.name}")
    assert not missing, (
        "перенесённая схема импортирует то, чего у нас нет, и это НЕ внесено в "
        "список осознанных пропусков:\n" + "\n".join(missing))


def test_the_table_context_slice_is_here_and_unguarded_calls_can_rely_on_it():
    """Срез ранжировщика на месте: его импорт в перенесённой схеме БЕЗ защиты.

    `_extract_tables_from_ocr_payload` импортирует `table_neighbor_context_lines`
    голым импортом и вызывается с боевого пути. Пропади модуль — прогон умрёт
    на любом документе с OCR-payload, а на пути станции (текст файлом) дефект
    не покажется вовсе: payload пуст, функция выходит раньше импорта.
    """
    from finengine.skills.loan_sector_table_ranker import table_neighbor_context_lines

    params = inspect.signature(table_neighbor_context_lines).parameters
    assert {"payload", "table_index", "pre_lines", "post_lines"} <= set(params), (
        f"сигнатура среза разошлась с местом вызова: {list(params)}")
