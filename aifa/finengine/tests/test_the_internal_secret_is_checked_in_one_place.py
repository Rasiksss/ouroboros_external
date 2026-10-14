"""Пустой внутренний секрет не открывает внутренние роуты молча.

ЦЕНА. Внутренние роуты бэкенда проверяли секрет копией одной строки в каждом:

    if tok and authorization != f"Bearer {tok}":

`if tok and ...` означает, что ПУСТОЙ секрет отключает проверку ЦЕЛИКОМ. Роуты
видны снаружи — `deploy/Caddyfile` проксирует `/api/*` на бэкенд, то есть
`https://<домен>/api/internal/doc-text/<id>` достижим из интернета. Дверь при
этом закрыта вторым рубежом (разовый нонс хода, 32 hex, 30 минут, владелец
сверяется), но замок снят.

Копий было ТРИ, а с личной методикой стало бы четыре — тот же класс, что
двойные enum'ы и приведение к году: правило в одном месте из нескольких.

ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ:
  1. правило живёт в ОДНОМ месте и все роуты зовут именно его;
  2. пустой секрет при поднятой строгости — 503, а не «проходи»;
  3. строгость выключена по умолчанию: локальный запуск без `.env` и тесты
     работают как раньше;
  4. непустой секрет сверяется дословно.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · вернуть `if tok and authorization != ...` в internal_docs → падает
    `test_an_empty_secret_closes_the_door_when_enforcement_is_on[doc-text]`;
  · `enforced()` → `return False` → падают все три параметризации того же теста;
  · снять 503 (пускать при пустом секрете) → падают те же три;
  · `require_gateway_token` перестаёт сверять непустой секрет → падает
    `test_a_wrong_secret_is_refused`.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from finengine.api import internal_auth

_TOKEN_ENV = "FINENGINE_OPENCLAW_GATEWAY_TOKEN"
_ENFORCE_ENV = "FINENGINE_ENFORCE_INTERNAL_AUTH"

# Роуты, которые обязаны спрашивать секрет одной и той же функцией. Личная
# методика приехала четвёртой 21.08 — и сторож поймал её копию проверки на
# мерже, ровно как было записано в docs/ОТЛОЖЕНО_ДО_АРХИТЕКТУРЫ_21-08.md.
_ROUTED = ["doc-text", "station-progress", "internal-tools", "methodology"]


def test_the_rule_lives_in_one_place():
    """Ни один роут не носит собственную копию проверки.

    Ищем не текст правила, а его СЛЕПОК: `if tok` рядом со сравнением
    заголовка. Копия вернётся именно в такой форме — она короче вызова.
    """
    import pathlib

    api = pathlib.Path(internal_auth.__file__).parent
    copies = []
    for path in sorted(api.glob("*.py")):
        if path.name == "internal_auth.py":
            continue
        body = path.read_text(encoding="utf-8")
        for line in body.splitlines():
            if "authorization" in line and "Bearer" in line and "if " in line:
                copies.append(f"{path.name}: {line.strip()}")
    assert not copies, (
        "проверка секрета скопирована мимо `internal_auth.require_gateway_token`:\n  "
        + "\n  ".join(copies))


@pytest.mark.parametrize("route", _ROUTED)
def test_an_empty_secret_closes_the_door_when_enforcement_is_on(route, monkeypatch):
    """Секрета нет, строгость поднята — 503, а не «проходи»."""
    monkeypatch.setenv(_TOKEN_ENV, "")
    monkeypatch.setenv(_ENFORCE_ENV, "1")
    with pytest.raises(HTTPException) as err:
        internal_auth.require_gateway_token("")
    assert err.value.status_code == 503, (
        f"[{route}] пустой секрет при поднятой строгости дал "
        f"{err.value.status_code} вместо 503 — развёртывание неполное, и об этом "
        "надо сказать, а не пускать")
    assert _TOKEN_ENV in str(err.value.detail), (
        "в отказе не названа переменная, которой не хватает — читателю нечего чинить")


def test_an_empty_secret_is_allowed_when_enforcement_is_off():
    """Умолчание не ломает локальный запуск и тесты.

    Строгость поднимается в compose dev и прода; здесь её нет, и поведение
    обязано остаться прежним, иначе первый же запуск без `.env` встанет без
    объяснения.
    """
    import os

    os.environ.pop(_ENFORCE_ENV, None)
    os.environ[_TOKEN_ENV] = ""
    internal_auth.require_gateway_token("")  # не бросает — это и проверяется


def test_a_wrong_secret_is_refused(monkeypatch):
    monkeypatch.setenv(_TOKEN_ENV, "правильный")
    monkeypatch.delenv(_ENFORCE_ENV, raising=False)
    with pytest.raises(HTTPException) as err:
        internal_auth.require_gateway_token("Bearer чужой")
    assert err.value.status_code == 401
    internal_auth.require_gateway_token("Bearer правильный")  # верный проходит


def test_the_right_secret_passes_under_enforcement(monkeypatch):
    """Строгость не мешает нормальной работе — иначе её никто не поднимет."""
    monkeypatch.setenv(_TOKEN_ENV, "правильный")
    monkeypatch.setenv(_ENFORCE_ENV, "1")
    internal_auth.require_gateway_token("Bearer правильный")


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("", False), ("0", False), ("false", False), ("нет", False),
])
def test_the_flag_reads_the_forms_a_person_would_write(raw, expected, monkeypatch):
    monkeypatch.setenv(_ENFORCE_ENV, raw)
    assert internal_auth.enforced() is expected
