"""Лейны шлюза модели разведены по ОКРУЖЕНИЯМ, а не только по моделям (#149).

ЗАЧЕМ. Раньше ключом лейна было имя модели, поэтому прод и стенд делили ОДИН
семафор и ОДНУ очередь. Прогон бенчмарка на стенде — это сотни параллельных
вызовов той же модели, что обслуживает живых пользователей aifaclaw.ru: очередь
заполнялась, шлюз отдавал 503, станция трижды повторяла и возвращала человеку
«извлечение не удалось». То есть нагрузка разработки физически доставала до
прода — и первый же шаг плана переезда (снятие базовой линии) положил бы его.

Здесь защищается ровно это свойство: у разных окружений РАЗНЫЕ лейны, и
переполнение очереди у одного не видно другому.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

INFPROXY = Path(__file__).resolve().parents[1] / "deploy" / "infproxy"


@pytest.fixture
def proxy(monkeypatch):
    """Свежий модуль шлюза с заданными переменными окружения.

    Модуль читает env на импорте, поэтому его приходится перезагружать: иначе
    тесты видели бы карту лейнов первого из них.
    """
    def _load(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sys.path.insert(0, str(INFPROXY))
        try:
            sys.modules.pop("app", None)
            return importlib.import_module("app")
        finally:
            sys.path.remove(str(INFPROXY))

    return _load


def test_lane_key_separates_environments(proxy):
    """Один и тот же токен → одна метка; разные токены → разные метки."""
    app = proxy(INFPROXY_TOKEN_LABELS="tok-prod=prod,tok-dev=dev")
    assert app._env_label("tok-prod") == "prod"
    assert app._env_label("tok-dev") == "dev"
    assert app._env_label("tok-prod") != app._env_label("tok-dev")


def test_unknown_token_still_gets_its_own_lane(proxy):
    """Карта меток нужна для ЧИТАЕМОСТИ, а не для изоляции.

    Даже без неё лейны обязаны разъехаться — иначе забытая настройка молча
    вернула бы общую очередь, и никто бы не заметил.
    """
    app = proxy(INFPROXY_TOKEN_LABELS="")
    a, b = app._env_label("aaaa1111"), app._env_label("bbbb2222")
    assert a != b, "разные токены обязаны давать разные лейны и без карты меток"


def test_no_token_is_not_confused_with_a_token(proxy):
    app = proxy(INFPROXY_TOKEN_LABELS="")
    assert app._env_label(None) == "default"
    assert app._env_label(None) != app._env_label("aaaa1111")


@pytest.mark.asyncio
async def test_environments_get_separate_semaphores(proxy):
    """Главное свойство: два окружения на ОДНОЙ модели — два разных семафора."""
    app = proxy(INFPROXY_TOKEN_LABELS="", INFPROXY_MAX_CONCURRENCY="3")
    sem_prod = await app._lane("prod|qwen/qwen3.6-plus", "qwen/qwen3.6-plus")
    sem_dev = await app._lane("dev|qwen/qwen3.6-plus", "qwen/qwen3.6-plus")
    assert sem_prod is not sem_dev, "прод и стенд снова делят один семафор"
    # Исчерпание лейна стенда не должно занимать ни одного слота у прода.
    for _ in range(3):
        await sem_dev.acquire()
    assert sem_dev.locked(), "лейн стенда должен быть исчерпан"
    assert not sem_prod.locked(), "лейн прода не должен пострадать от нагрузки стенда"


@pytest.mark.asyncio
async def test_same_environment_shares_one_lane(proxy):
    """Изоляция не должна превратиться в «каждому вызову свой лейн»."""
    app = proxy(INFPROXY_TOKEN_LABELS="")
    a = await app._lane("prod|m", "m")
    b = await app._lane("prod|m", "m")
    assert a is b


@pytest.mark.asyncio
async def test_limit_can_be_set_per_environment_and_falls_back_to_model(proxy):
    """Точный предел окружения важнее общего — иначе сумма лейнов вырастет вдвое.

    Разведение лейнов само по себе УДВАИВАЕТ потенциальную нагрузку на
    провайдера (было 20 на модель, стало 20+20). Поэтому нужен способ задать
    пределы раздельно, а общий предел остаётся запасным.
    """
    app = proxy(
        INFPROXY_TOKEN_LABELS="",
        INFPROXY_MODEL_CONCURRENCY="prod|qwen=12,qwen=8",
        INFPROXY_MAX_CONCURRENCY="4",
    )
    await app._lane("prod|qwen", "qwen")
    await app._lane("dev|qwen", "qwen")
    await app._lane("prod|other", "other")
    assert app._lane_conc["prod|qwen"] == 12, "точный предел окружения не применился"
    assert app._lane_conc["dev|qwen"] == 8, "общий предел модели не применился как запасной"
    assert app._lane_conc["prod|other"] == 4, "дефолт не применился"


def test_queue_check_is_per_lane_not_per_model():
    """Переполнение очереди у стенда не должно отдавать 503 проду.

    Проверяется по исходнику: счётчик ожидания и порог считаются по ключу
    лейна. Тест на поведение потребовал бы поднять сервер и залить очередь —
    здесь достаточно того, что ключ именно лейновый.
    """
    src = (INFPROXY / "app.py").read_text(encoding="utf-8")
    assert "_waiting[lane_key] >= MAX_QUEUE" in src, "очередь снова считается по модели"
    assert "sem = await _lane(lane_key, model)" in src
