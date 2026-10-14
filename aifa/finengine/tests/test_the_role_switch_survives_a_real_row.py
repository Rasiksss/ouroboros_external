"""Смена роли переживает НАСТОЯЩУЮ строку прогона (Б2).

ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «в выпадашке роль меняется, таблица НЕ перестраивается».

ЧТО ОКАЗАЛОСЬ НА САМОМ ДЕЛЕ. Разбор шёл не по догадке, а проигрыванием таблицы
прогона Северстали (122 строки, взяты из базы прогонов) через схему ручки
`POST /metrics/format-role`. Схема отбила запрос: 302 ошибки валидации —
`segment` и `dimension` равны null во ВСЕХ 122 строках, `source_form` — в 58.
Поля объявлены как `str`, и pydantic отказывает на null.

То есть перестроение отбивалось 422-й ВСЕГДА: при любой роли, любом документе
и любом пользователе. Раскладки ролей при этом исправны — на тех же 122 строках
ДИЭ даёт 22 строки, CIB 260, Риски 22.

ЭТО ВТОРАЯ СЕРИЯ ОДНОГО И ТОГО ЖЕ ДЕФЕКТА. Первая (#142, 17.08) была про
значения: `float | None` валил запрос на строковой величине аудиторского
мнения. Тогда починили значения и не посмотрели на текстовые поля рядом.

ПОЧЕМУ СТОРОЖ ИМЕННО ТАКОЙ. Строку для проверки НЕ пишем руками: берём её у
`station_turn._row_body` — того самого кода, который отдаёт строки на экран.
Фронт присылает обратно ровно их. Пока обе стороны берут форму из одного места,
сторож ловит расхождение при первом же изменении контракта.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать валидатор `_absent_text_is_empty` из
`MetricsExportRow` — тест обязан покраснеть и назвать поля.

ЧЕГО НЕ ЛОВИТ (ревизия 21.08.2026). Три теста из четырёх ЗАПУСКАЮТ схему,
раскладку и `_row_body`; четвёртый — `test_the_front_really_sends_these_fields`
— читает `.tsx` глазами grep'а и ищет `поле: r.поле`. Он оставлен как паритет
двух мест (бэкенд ↔ фронт), но знать про него надо вот что: замер 21.08 —
переименование переменной строки в `.map((r) => …)` на `row` БЕЗ правки самих
полей оставляет его зелёным, хотя такой фронт не собирается вовсе. Он ловит
пропажу поля из тела запроса и не ловит ничего про работоспособность фронта:
ни компиляции, ни выполнения здесь нет.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finengine.api import station_turn
from finengine.api.main import MetricsExportRow, MetricsRoleFormatRequest

ROOT = Path(__file__).resolve().parents[1]
PANE = ROOT.parent / "web" / "src" / "components" / "workspace" / "panes" / "extracted-pane.tsx"

# Событие движка ровно той формы, что приезжает со станции: пустые разрезы —
# null, а не пустая строка. Так их пишет `two_stage_extractor`, так они лежат
# в базе прогонов и так уходят на экран.
ENGINE_EVENT = {
    "type": "metric",
    "enum": "MODIFIED_AUDITORS_OPINION",
    "name_ru": "Наличие модифицированного мнения аудитора",
    "value_current": None,
    "value_prev": None,
    "period_values": [{"page": 4, "unit": "boolean", "value": None, "period": "2025"}],
    "unit": "",
    "found": False,
    "is_calculated": False,
    "formula": "",
    "page": 4,
    "segment": None,
    "dimension": None,
}


def _body(rows: list[dict], role: str = "die") -> dict:
    return {
        "company_name": "Северсталь", "company_type": "non_financial", "role": role,
        "period_current": "2025", "period_prev": "2024", "unit": "млн. руб.",
        "rows": rows,
    }


def _as_frontend_sends(event: dict) -> dict:
    """Строка экрана → тело запроса: фронт шлёт обратно поля, которые получил."""
    row = station_turn._row_body(event)
    return {k: v for k, v in row.items() if k in MetricsExportRow.model_fields}


def test_the_screen_row_comes_back_through_the_role_route():
    """ГЛАВНОЕ: строка, уехавшая на экран, принимается обратно без 422."""
    row = _as_frontend_sends(ENGINE_EVENT)
    assert row["segment"] is None and row["dimension"] is None, (
        "предпосылка ушла: строка экрана больше не несёт null в разрезах — "
        "проверять стало нечего, сторож надо переписать под новый контракт")
    MetricsRoleFormatRequest.model_validate(_body([row]))


@pytest.mark.parametrize("field", ["segment", "dimension", "source_form", "name_ru",
                                   "unit", "source_text", "formula"])
def test_every_text_field_of_the_row_tolerates_absence(field: str):
    """Пустое и отсутствующее для текстового поля строки — одно и то же."""
    row = {**_as_frontend_sends(ENGINE_EVENT), field: None}
    req = MetricsRoleFormatRequest.model_validate(_body([row]))
    assert getattr(req.rows[0], field) == "", (
        f"поле {field} приняли, но не привели к пустой строке — форматтер роли "
        f"получит None там, где ждёт текст")


def test_the_front_really_sends_these_fields():
    """СТОРОЖ ДОСТАВКИ: проверяем то, что фронт шлёт, а не что удобно проверять."""
    src = PANE.read_text(encoding="utf-8")
    body = src.split("void formatMetricsForRole(", 1)[1].split("})", 1)[0]
    for field in ("segment", "dimension", "source_form", "unit", "formula"):
        assert f"{field}: r.{field}" in body, (
            f"фронт больше не шлёт {field} — либо контракт разъехался, либо "
            f"сторож проверяет несуществующий случай")


def test_the_layouts_still_build_from_such_rows():
    """Схема приняла — раскладка обязана собраться, иначе починили не то."""
    from finengine.roles.formatter import build_formatted_table
    from finengine.roles.scope import build_role_plan

    rows = [
        _as_frontend_sends(ENGINE_EVENT),
        _as_frontend_sends({**ENGINE_EVENT, "enum": "SALES_REVENUE",
                            "name_ru": "Выручка", "value_current": 178733.0,
                            "found": True, "unit": "млн руб.",
                            "period_values": [{"period": "2025", "value": 178733.0,
                                               "unit": "млн руб.", "page": 5}]}),
    ]
    req = MetricsRoleFormatRequest.model_validate(_body(rows))
    plan = build_role_plan("die", "non_financial")
    table = build_formatted_table(
        plan=plan, metrics=[r.model_dump() for r in req.rows],
        meta={"period_current": "2025", "period_prev": "2024", "unit": "млн руб.",
              "company_type": "non_financial", "company_name": "Северсталь"})
    assert table and table["rows"], "раскладка не собралась из принятых строк"
