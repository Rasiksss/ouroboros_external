"""`_calculate_metrics` считает расчётные формулы в ТОПОЛОГИЧЕСКОМ порядке
зависимостей (граф строится из поля `deps`), а не фиксированным числом
проходов по списку в порядке реестра.

ИСТОРИЯ ЭТОГО ФАЙЛА. До миграции на топологический порядок здесь стоял
характеризационный тест, фиксировавший РЕАЛЬНЫЙ ЛАТЕНТНЫЙ ДЕФЕКТ старого
подхода ("multi-pass for chained dependencies", `for _ in range(3)`): цепочка
зависимостей глубиной 4 в невыгодном порядке списка молча теряла четвёртый
уровень (`None` вместо посчитанного числа) — ровно тот класс случая, для
которого в файле уже была ручная заплатка-«четвёртый проход», написанная
специально под EBITDA-зависимые метрики. Тест ловил это на синтетической
цепочке `L2 = L1*2, L3 = L2*2, ..., L{depth+1} = L{depth}*2`.

Теперь, когда обход топологический, глубина цепочки и порядок её объявления в
реестре больше не имеют значения — тесты ниже это ПРОВЕРЯЮТ, а не
характеризуют дефект.
"""
from __future__ import annotations

from finengine.skills import bulk_extractor as bx


def _chain_calc_defs(depth: int) -> list[tuple[str, str, str, list[str]]]:
    """L2 = L1*2, L3 = L2*2, ..., L{depth+1} = L{depth}*2.

    Порядок в списке — ОБРАТНЫЙ порядку зависимостей (L{depth+1} первым,
    L2 последним): именно так реестр может выглядеть, если его наполняли не
    по топологии, а по алфавиту/категории.
    """
    return [
        (f"L{n}", f"L{n}", f"L{n - 1} * 2", [f"L{n - 1}"])
        for n in range(depth + 1, 1, -1)
    ]


def test_three_levels_resolve_regardless_of_list_order():
    """Цепочка в три расчётных уровня досчитывается целиком в худшем
    (обратном) порядке списка — было верно и до миграции, остаётся верно."""
    calc_defs = _chain_calc_defs(depth=3)  # список: L4, L3, L2
    result = bx._calculate_metrics(calc_defs, extracted={"L1": 10.0})
    assert result["L2"] == 20.0
    assert result["L3"] == 40.0
    assert result["L4"] == 80.0


def test_a_fourth_chained_level_in_reverse_order_now_resolves():
    """ПРОВЕРКА ФИКСА: цепочка в четыре расчётных уровня, поданная в обратном
    порядке (L5, L4, L3, L2) — до миграции именно на этом входе движок молча
    терял L5 (см. докстринг файла). Топологический порядок не зависит от
    порядка списка: L5 обязан посчитаться корректно.
    """
    calc_defs = _chain_calc_defs(depth=4)  # список: L5, L4, L3, L2
    result = bx._calculate_metrics(calc_defs, extracted={"L1": 10.0})
    assert result["L2"] == 20.0
    assert result["L3"] == 40.0
    assert result["L4"] == 80.0
    assert result["L5"] == 160.0


def test_order_independence_forward_and_reverse_agree():
    """Тот же четырёхуровневый расчёт в ПРЯМОМ порядке (L2, L3, L4, L5) даёт
    ровно тот же результат, что и обратный в тесте выше — предмет проверки
    именно ОТСУТСТВИЕ зависимости от порядка списка, а не глубина цепочки
    как таковая."""
    calc_defs = list(reversed(_chain_calc_defs(depth=4)))  # список: L2, L3, L4, L5
    result = bx._calculate_metrics(calc_defs, extracted={"L1": 10.0})
    assert result["L5"] == 160.0


def test_a_dependency_cycle_does_not_hang_and_is_reported(caplog):
    """Реестр сегодня циклов не содержит, но формула — текст, который правит
    не код: если он однажды опишет цикл (A зависит от B, B — от A), обход не
    обязан зависнуть или уронить прогон — оба узла остаются `None`, и находка
    называется явно в логе, а не молчит.
    """
    calc_defs = [
        ("A", "A", "B * 2", ["B"]),
        ("B", "B", "A * 2", ["A"]),
    ]
    with caplog.at_level("WARNING"):
        result = bx._calculate_metrics(calc_defs, extracted={})
    assert result == {"A": None, "B": None}
    assert "циклическая" in caplog.text
