"""Каждый производитель расчётных строк кладёт в строку её входы.

ЦЕНА (Б5, 19.08.2026). Производителей расчётной строки в репозитории ПЯТЬ, а не
два, как считала разведка, и не три, как считала первая редакция этого сторожа.
Кроме `ratios.py` (навык расчёта), `two_stage_extractor.py` (боевая схема) и
`streams_full_context.py` (вторая схема извлечения, включается настройкой
`FINENGINE_EXTRACT_ARCH`) расчётные строки отдают ещё две схемы — `aifa` и
`aifa_baseline`. Они не мёртвые: роут `/documents/{id}/extract/stream` выбирает
их параметром `arch` (`api/main.py`, ветки `aifa_baseline` / `aifa_dev`), а
выбор живёт в выпадашке «Агент» на фронте (`composer.tsx`,
`chat-header-controls.tsx`). Пропустить производителя значило бы получить
таблицу, у которой происхождение расчёта то есть, то нет, — в зависимости от
выпадашки, то есть невоспроизводимо.

Список путей — константа `PRODUCERS`, и она сама по себе слепа к новому файлу.
Поэтому её сопровождает `test_no_calculated_row_producer_is_missing_from_the_list`:
он ищет литералы расчётной строки по всему `src/finengine` и падает, если
нашёлся файл вне списка.

ПОЧЕМУ СТРУКТУРНАЯ ПРОВЕРКА, А НЕ ПРОГОН. Обе движковых схемы — асинхронные
генераторы, которым нужен текст отчёта и вызов модели. Прогонять их в наборе
тестов нечем, а форму строки они собирают литералом словаря — его и читаем
через `ast`, а не поиском по тексту: комментарий со словом `inputs` такую
проверку не озеленит.

ПРОВЕРКА СТОРОЖА ОТКАТОМ (проделана 19.08): убрать `"inputs"` из литерала
расчётной строки в любом из трёх файлов — тест краснеет и называет файл.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ (ревизия 21.08, проверено мутацией). Он читает
ФОРМУ литерала, а не выдачу: заменить `"inputs": prov.get("inputs") or []` на
`"inputs": []` в `ratios.py` — ключ на месте, все пять параметров зелёные, а
происхождение расчёта пустое у КАЖДОЙ строки. Ту же мутацию ловят три
исполняющих теста в `test_calculated_row_signature_names_its_column.py`, и
только для `ratios.py`: у четырёх остальных производителей (обе движковые
схемы + `aifa`/`aifa_baseline`) исполняющего соседа нет вовсе. То есть здесь
охраняется НАЛИЧИЕ ПОДПИСИ, а её НЕПУСТОТА — нигде.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "finengine" / "skills"

# ⚠️ ПРОИЗВОДИТЕЛЕЙ СНОВА ТРИ (21.08.2026). Было пять: `aifa/extractor.py` и
# `aifa_baseline/extractor.py` снесены вместе со своими режимами — они брали
# формулы из пятой копии реестра и после перевода доходностей в проценты
# отдавали числа в сто раз меньше. Правило «завёл в одном из N — дефект»
# остаётся, но N теперь три, и это ЛУЧШЕ: два места для одного знания ушло.
PRODUCERS = (
    SRC / "ratios.py",
    SRC / "two_stage_extractor.py",
    SRC / "bulk_extractor" / "streams_full_context.py",
)


def _calculated_row_literals(дерево: ast.Module) -> list[ast.Dict]:
    """Литералы словаря, у которых `"is_calculated": True` — то есть строки расчёта."""
    out: list[ast.Dict] = []
    for node in ast.walk(дерево):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "is_calculated"
                and isinstance(value, ast.Constant)
                and value.value is True
            ):
                out.append(node)
    return out


@pytest.mark.parametrize("path", PRODUCERS, ids=lambda p: p.name)
def test_every_calculated_row_literal_has_inputs(path: Path, движок_ast):
    rows = _calculated_row_literals(движок_ast[path])
    assert rows, f"{path.name}: литерал расчётной строки не найден — тест ослеп"
    for row in rows:
        keys = {k.value for k in row.keys if isinstance(k, ast.Constant)}
        assert "inputs" in keys, (
            f"{path.name}, строка {row.lineno}: расчётная строка без `inputs` — "
            "происхождение формулы до экрана не доедет"
        )


def test_no_calculated_row_producer_is_missing_from_the_list(движок_ast):
    """Сам список путей — слепое место: шестого производителя он не заметит.

    Ровно так и вышло 19.08: сторож перечислял три файла, а схем было пять, и
    две живые остались без подписи расчёта. Поэтому список сверяем с поиском по
    всему пакету, а не доверяем ему на слово.

    `движок_ast` (conftest.py) несёт и station-skills/ — фильтруем на package
    (src/finengine/), как раньше делал собственный rglob.
    """
    package = SRC.parent
    found = {
        path
        for path, tree in движок_ast.items()
        if package in path.parents
        and not path.name.startswith("test_")
        and _calculated_row_literals(tree)
    }
    assert found, "литералов расчётной строки не найдено нигде — тест ослеп"
    missing = sorted(str(p.relative_to(package)) for p in found - set(PRODUCERS))
    assert not missing, (
        "производитель расчётных строк вне списка PRODUCERS — его подпись никто "
        f"не сторожит: {missing}"
    )
