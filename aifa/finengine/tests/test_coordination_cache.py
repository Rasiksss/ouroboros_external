"""Общий кэш координации: одно поведение на обе реализации.

🔴 ЗАЧЕМ (решение владельца 25.08.2026). `docs/rules/architecture.md` 3.2
(🔴 MUST): «Stateless-реплики… In-process синглтоны/счётчики/локи,
разъезжающиеся на `>1` реплике — баги. Сессии/SSE-состояние → Redis».

По пакету таких мест было восемь. Пять — обычные «ключ → значение + срок»;
три держат живые asyncio-объекты и механически не переносятся (почему — в
шапке `store/cache.py`).

⚠️ REDIS-ВЕТКА ЗДЕСЬ НЕ ПРОВЕРЯЕТСЯ ЖИВЬЁМ: для неё нужен поднятый Redis, а
тесты идут без сети. Проверяется контракт локальной ветки и переключатель.
Живая проверка — смоук на стенде, и это сказано вслух, а не спрятано за
зелёным прогоном.
"""
from __future__ import annotations

import time

import pytest

from finengine.store.cache import LocalCache, get_cache


@pytest.fixture
def кэш() -> LocalCache:
    return LocalCache()


def test_a_value_survives_a_round_trip(кэш):
    кэш.set("k", {"а": 1, "б": ["x", "y"]}, 60)
    assert кэш.get("k") == {"а": 1, "б": ["x", "y"]}


def test_a_missing_key_is_none_not_an_error(кэш):
    assert кэш.get("нет-такого") is None


def test_an_expired_value_is_gone(кэш):
    """Срок — не украшение: пропуск в комнату, переживший ход, однажды подберут."""
    кэш.set("k", "v", 0.02)
    time.sleep(0.05)
    assert кэш.get("k") is None
    assert "k" not in кэш.keys()


def test_a_live_value_is_not_swept_by_a_neighbours_expiry(кэш):
    """Подметание чужого не должно задевать живое."""
    кэш.set("живой", "v", 60)
    кэш.set("умрёт", "v", 0.02)
    time.sleep(0.05)
    кэш.get("живой")  # обращение запускает подметание
    assert кэш.get("живой") == "v"
    assert кэш.keys() == ["живой"]


def test_keys_are_filtered_by_prefix(кэш):
    """На префиксах стоит разделение соседей: скоуп документов, методика, сжатие.

    Пересекись они — сброс одного реестра унёс бы чужой.
    """
    кэш.set("docscope:n1", ["d"], 60)
    кэш.set("methowner:n1", 7, 60)
    assert кэш.keys("docscope:") == ["docscope:n1"]
    assert кэш.keys("methowner:") == ["methowner:n1"]
    assert len(кэш.keys()) == 2


def test_delete_removes_only_its_own(кэш):
    кэш.set("a", 1, 60)
    кэш.set("b", 2, 60)
    кэш.delete("a")
    assert кэш.get("a") is None and кэш.get("b") == 2


def test_overwrite_replaces_and_refreshes_the_deadline(кэш):
    кэш.set("k", "старое", 0.02)
    кэш.set("k", "новое", 60)
    time.sleep(0.05)
    assert кэш.get("k") == "новое", "перезапись не продлила срок"


def test_the_default_stays_in_process_until_someone_sets_the_url():
    """Код ничего не включает сам: пустой адрес = память процесса, как было."""
    from finengine.config import settings
    assert settings.redis_url == ""
    assert type(get_cache()).__name__ == "LocalCache"


def test_the_moved_registries_use_the_shared_cache():
    """Пять переехавших реестров спрашивают ОБЩИЙ кэш, а не свой словарь.

    Сторож на форму: у модуля не должно остаться изменяемой глобали-словаря
    под старым именем. Проверяем именно отсутствие — вернётся словарь, вернётся
    и расхождение реплик.
    """
    from finengine.api import internal_compression_cache as comp
    from finengine.api import internal_docs, internal_methodology as meth

    for модуль, имена in ((internal_docs, ("_scope",)),
                          (meth, ("_owners", "_served", "_spent_one_shot")),
                          (comp, ("_store",))):
        for имя in имена:
            assert not hasattr(модуль, имя), (
                f"{модуль.__name__}.{имя} вернулся — состояние снова в памяти процесса")


def test_the_registries_answer_through_the_door_not_the_window():
    """У переехавших есть ПУБЛИЧНЫЙ доступ: тесты не должны лезть внутрь.

    Куплено этим же переездом: шесть фикстур звали `internal_docs._scope.clear()`
    и упали разом, как только хранилище сменилось.
    """
    from finengine.api import internal_compression_cache as comp
    from finengine.api import internal_docs, internal_methodology as meth

    for модуль, имя in ((internal_docs, "reset_scopes"), (internal_docs, "nonce_scopes"),
                        (meth, "reset"), (meth, "expire_nonce"), (meth, "set_served"),
                        (comp, "clear"), (comp, "entries")):
        assert callable(getattr(модуль, имя, None)), (
            f"у {модуль.__name__} нет публичного {имя}() — тестам придётся лезть внутрь")
