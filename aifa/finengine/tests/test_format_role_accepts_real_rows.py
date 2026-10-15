"""Ручка /metrics/format-role принимает СТРОКИ ТАКИМИ, КАКИЕ ОНИ ЕСТЬ (#142).

КАК НАШЛОСЬ (17.08). Владелец днями спрашивал, почему выбор роли в выпадашке
«Извлечь метрики» не перестраивает таблицу. Вся проводка на фронте была на
месте: клик → POST /metrics/format-role → FormattedTable. Разгадка нашлась в
access-логе бэкенда: два 422 с адреса владельца.

Схема требовала `value_current: float | None`, а фронт держит
MetricValue = number | string | null — движок отдаёт и сырые ячейки строками,
и «да/нет» аудиторского мнения. Первая же строковая величина валила ВЕСЬ
запрос, `.catch()` на фронте молчал, таблица оставалась общей.

Урок здесь не про типы, а про молчание: отказ жил только в access-логе, и
симптом выглядел как «фронт ничего не делает».
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import finengine.api.main as m
from finengine.api.main import get_current_user
from finengine.auth.db import User


@pytest.fixture()
def client():
    m.app.dependency_overrides[get_current_user] = lambda: User(id=1, username="t", is_admin=True)
    try:
        yield TestClient(m.app)
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)


def _rows_like_the_real_run():
    """Строки в том виде, в каком их шлёт фронт с живого прогона ОТП."""
    return [
        # Аудиторское мнение — движок отдаёт «нет», не число.
        {"enum": "MODIFIED_AUDITORS_OPINION", "name_ru": "Наличие модифицированного мнения",
         "value_current": "нет", "value_prev": None},
        # Сырая ячейка строкой — так приезжает из period_values.
        {"enum": "NET_PROFIT", "name_ru": "Чистая прибыль",
         "value_current": "78 568,37", "value_prev": 43116.77,
         "period_values": [{"period": "FY 2024", "raw_period": "31 декабря 2024",
                            "period_normalization_reason": "x", "value": 78568.37}]},
        # Обычное число — прежний счастливый путь обязан выжить.
        {"enum": "TOTAL_ASSETS", "name_ru": "Итого активы",
         "value_current": 617811.03, "value_prev": 360868.28},
    ]


def test_string_values_do_not_422(client):
    got = client.post("/metrics/format-role", json={
        "role": "die", "company_type": "bank", "company_name": "ОТП Банк",
        "period_current": "FY 2024", "period_prev": "FY 2023",
        "rows": _rows_like_the_real_run(),
    })
    assert got.status_code == 200, f"{got.status_code}: {got.text[:300]}"
    assert got.json().get("formatted"), "раскладка роли не вернулась"


def test_all_roles_accept_the_same_rows(client):
    """Каждая роль обязана переварить один и тот же живой набор строк:
    ломаться на роли — значит молча вернуть пользователю общую таблицу."""
    for role in ("risks", "baifp", "die", "cib", "dgr"):
        got = client.post("/metrics/format-role", json={
            "role": role, "company_type": "bank", "rows": _rows_like_the_real_run(),
        })
        assert got.status_code == 200, f"{role}: {got.status_code} {got.text[:200]}"
