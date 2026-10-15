"""`dataset_from_metrics` — сборка набора величин из строк движка.

🔴 ИЗ ЭТОГО ФАЙЛА СНЯТО ОДИННАДЦАТЬ ТЕСТОВ ИЗ ДВЕНАДЦАТИ (29.08.2026).

Они гоняли `calc.compute` и `calc.resolve_dataset` — движок тула `fe_calc`,
который считал формулы В ПРОЦЕССЕ БЭКЕНДА. Тул снят решением владельца:
исполняться должно в станции, а не рядом с базой, S3 и ключами. Вместе с ним
снялись обе функции, и тесты на них остались без предмета.

Осталась `dataset_from_metrics`: её зовёт `analysis/charts.py`, и её правило
«сегментные строки в набор не идут» другими тестами не покрыто — проверено
поиском по `tests/`.
"""
from __future__ import annotations



from finengine.skills import calc


def _metrics():
    return [
        {"enum": "NET_INCOME", "name_ru": "Чистая прибыль", "value_current": 1938422.0,
         "page": 9, "unit": "тыс. руб.",
         "period_values": [{"period": "FY 2024", "value": 1938422.0},
                           {"period": "FY 2023", "value": 1088000.0}]},
        {"enum": "SALES_REVENUE", "name_ru": "Выручка", "value_current": 10000000.0,
         "period_values": [{"period": "FY 2024", "value": 10000000.0}]},
        # Сегментная строка: в набор попасть НЕ должна — иначе одно имя будет
        # значить разное в зависимости от того, что попалось раньше.
        {"enum": "SALES_REVENUE", "dimension": "operating_segment", "segment": "Ритейл",
         "value_current": 4000000.0, "period_values": [{"period": "FY 2024", "value": 4000000.0}]},
    ]


def test_dataset_ignores_segment_rows():
    ds = calc.dataset_from_metrics(_metrics())
    assert ds["SALES_REVENUE"]["main"] == 10000000.0, "сегмент не должен подменять итог"
