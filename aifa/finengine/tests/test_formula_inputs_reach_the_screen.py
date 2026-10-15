"""Входы формулы доезжают до экрана и возвращаются с него без 422.

ЦЕНА (Б5, 19.08.2026). Вычислитель начал отдавать числа, по которым посчитана
формула, а производители строк — класть их в строку. Но между строкой станции
и экраном стоят ДВА среза по белому списку, и каждый молча выбрасывает всё
незнакомое:

  * `station_turn._ROW_KEYS` — срез события хода в тело строки экрана;
  * `MetricsExportRow` — схема, через которую фронт присылает строки ОБРАТНО
    (ручка `/metrics/format-role` и книга `/metrics/export.xlsx`).

Ровно на этом классе уже теряли поля: #142 — дважды подряд, и оба раза дефект
был виден только в access-логе.

ОТДЕЛЬНО ПРО СТАРЫЕ ПРОГОНЫ. Таблицы, снятые до этой правки, входов не несут,
и срез отдаёт по ключу null. Схема с `list[dict]` отбила бы такую строку 422-й
— то есть выбор роли сломался бы на ВСЕХ старых прогонах.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана 19.08):
  1. убрать `"inputs"` из `_ROW_KEYS` → падает `test_inputs_survive…`;
  2. убрать поле `inputs` из `MetricsExportRow` → падает `test_inputs_come_back…`;
  3. снять валидатор `_absent_inputs_are_empty` → падает `test_a_run_without…`;
  4. убрать `"inputs"` из сборки строк в `bulk_extractor/streams_sync.py` →
     падает `test_the_non_streaming_route…`.

СРЕЗОВ ОКАЗАЛОСЬ ТРИ, А НЕ ДВА. Третий — сборка ответа непотоковой ручки
`POST /documents/{doc_id}/extract` (`streams_sync.py`): она тоже перечисляет
ключи строки поимённо, а не копирует событие.
"""
from __future__ import annotations

import asyncio

from finengine.api import station_turn
from finengine.api.main import MetricsExportRow, MetricsRoleFormatRequest

INPUTS = [
    {"enum": "OPERATING_PROFIT", "value": 28355.0, "substituted_zero": False},
    {"enum": "DEPRECIATION_AMORTIZATION", "value": 9495.0, "substituted_zero": False},
]

CALCULATED_EVENT = {
    "type": "metric",
    "enum": "EBITDA",
    "name_ru": "EBITDA",
    "value_current": 37850.0,
    "value_prev": None,
    "period_values": [{"period": "2025", "value": 37850.0, "unit": "млн ₽", "page": None}],
    "unit": "млн ₽",
    "found": True,
    "is_calculated": True,
    "formula": "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION",
    "inputs": INPUTS,
    "page": None,
    "segment": None,
    "dimension": None,
}


def _body(rows: list[dict]) -> dict:
    return {
        "company_name": "Северсталь", "company_type": "non_financial", "role": "die",
        "period_current": "2025", "period_prev": "2024", "unit": "млн ₽",
        "rows": rows,
    }


def _as_frontend_sends(event: dict) -> dict:
    row = station_turn._row_body(event)
    return {k: v for k, v in row.items() if k in MetricsExportRow.model_fields}


def test_inputs_survive_the_screen_row_slice():
    assert station_turn._row_body(CALCULATED_EVENT)["inputs"] == INPUTS


def test_inputs_come_back_from_the_screen_into_the_role_route():
    req = MetricsRoleFormatRequest.model_validate(_body([_as_frontend_sends(CALCULATED_EVENT)]))
    assert req.rows[0].inputs == INPUTS


def test_a_run_without_inputs_still_switches_its_role():
    """Прогон, снятый до Б5: ключа в событии нет, срез отдаёт null."""
    legacy = {k: v for k, v in CALCULATED_EVENT.items() if k != "inputs"}
    row = _as_frontend_sends(legacy)
    assert row["inputs"] is None, (
        "предпосылка ушла: срез больше не отдаёт null по отсутствующему ключу — "
        "сторож надо переписать под новый контракт")
    req = MetricsRoleFormatRequest.model_validate(_body([row]))
    assert req.rows[0].inputs == []


def test_the_non_streaming_route_keeps_the_inputs_too(monkeypatch):
    """Сборка ответа `POST /documents/{doc_id}/extract` — третий срез по
    белому списку.

    Прогоняем сборку, а не читаем её литерал: `"inputs": []` вместо
    `event.get("inputs")` оставляло разбор по `ast` зелёным (ключ на месте),
    а входы формулы при этом терялись — мутация проверена 21.08.
    """
    from finengine.skills.bulk_extractor import streams_sync

    async def _stream(doc_id, company_type, index=None):
        yield CALCULATED_EVENT
        yield {"type": "done", "company_type": "non_financial", "unit": "млн ₽"}

    class _Doc:
        company_name = "Северсталь"
        period = "2025"

    class _Index:
        def get_document(self, doc_id):
            return _Doc()

    monkeypatch.setattr(streams_sync, "_extract_stream_full_context", _stream)
    out = asyncio.run(streams_sync._extract_async("d1", "non_financial", index=_Index()))
    assert out["metrics"][0]["inputs"] == INPUTS, (
        "непотоковая ручка режет входы формулы — происхождение расчёта "
        "теряется на её пути")
