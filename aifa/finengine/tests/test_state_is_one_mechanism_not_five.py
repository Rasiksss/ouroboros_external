"""Именованное состояние: один механизм на пять видов, обе реализации равны.

🔴 ЗАЧЕМ ЭТОТ ФАЙЛ. Переезжая с тома, я написал подряд три хранилища одной
формы — сессии, артефакты, журналы, — и на четвёртом (списки наблюдения,
личная методика, состояние пользователя) стало видно: это одна вещь. Пятый
самодельный «(владелец, ключ) → JSON» завёл бы ровно тот дефект, ради ухода
от которого переезд и делается.

Проверки идут на ОБЕИХ реализациях одним набором: расхождение здесь дефект, а
не особенность бэкенда. У сессий такой же набор сразу поймал разошедшееся
поведение первичного ключа.
"""
from __future__ import annotations

import pytest

from finengine.store import state as S


@pytest.fixture(params=["файлы", "база"])
def хранилище(request, tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    if request.param == "файлы":
        return S.LocalStateStore()
    # Базу спрашиваем ЗДЕСЬ, а не в сигнатуре: вариант «файлы» её не
    # требует, и общий запрос пропускал бы файловую ветку без причины.
    return S.SqlStateStore(request.getfixturevalue('бд'))


def test_a_document_survives_a_round_trip(хранилище):
    хранилище.put(S.WATCHLIST, "7", "проект", {"ru": ["Сбер"], "world": []})

    assert хранилище.get(S.WATCHLIST, "7", "проект") == {"ru": ["Сбер"], "world": []}


def test_another_user_does_not_see_the_document(хранилище):
    """🔴 ГЛАВНОЕ СВОЙСТВО. Мутация: убрать `owner_key` из условия — краснеет."""
    хранилище.put(S.WATCHLIST, "7", "проект", {"ru": ["Сбер"]})

    assert хранилище.get(S.WATCHLIST, "8", "проект") is None
    assert хранилище.keys(S.WATCHLIST, "8") == []


def test_another_user_cannot_overwrite_the_document(хранилище):
    """Владелец в условии ЗАПИСИ тоже: иначе чужой ход затрёт чужой документ."""
    хранилище.put(S.WATCHLIST, "7", "проект", {"ru": ["Сбер"]})
    хранилище.put(S.WATCHLIST, "8", "проект", {"ru": ["чужое"]})

    assert хранилище.get(S.WATCHLIST, "7", "проект") == {"ru": ["Сбер"]}


def test_kinds_do_not_leak_into_each_other(хранилище):
    """Пять видов в одной таблице — и не смешиваются."""
    хранилище.put(S.SESSION, "7", "к", {"что": "сессия"})
    хранилище.put(S.WATCHLIST, "7", "к", {"что": "список"})
    хранилище.put(S.METHODOLOGY, "7", "к", {"что": "методика"})

    assert хранилище.get(S.SESSION, "7", "к") == {"что": "сессия"}
    assert хранилище.get(S.WATCHLIST, "7", "к") == {"что": "список"}
    assert хранилище.get(S.METHODOLOGY, "7", "к") == {"что": "методика"}


def test_writing_twice_replaces_rather_than_duplicates(хранилище):
    хранилище.put(S.USER_STATE, "7", "sources", {"версия": 1})
    хранилище.put(S.USER_STATE, "7", "sources", {"версия": 2})

    assert хранилище.get(S.USER_STATE, "7", "sources") == {"версия": 2}
    assert хранилище.keys(S.USER_STATE, "7") == ["sources"]


def test_deleting_says_whether_there_was_anything(хранилище):
    assert хранилище.delete(S.USER_STATE, "7", "нет") is False
    хранилище.put(S.USER_STATE, "7", "есть", {})
    assert хранилище.delete(S.USER_STATE, "7", "есть") is True
    assert хранилище.get(S.USER_STATE, "7", "есть") is None


def test_a_missing_document_is_none_not_a_crash(хранилище):
    assert хранилище.get(S.METHODOLOGY, "7", "нет-такого") is None


# ─────────────── «прочитать → изменить → записать» ───────────────


def test_mutate_sees_what_is_already_there(хранилище):
    хранилище.put(S.METHODOLOGY, "7", "overlay", {"deltas": [{"id": "a"}]})

    итог = хранилище.mutate(S.METHODOLOGY, "7", "overlay",
                            lambda d: {"deltas": [*d["deltas"], {"id": "b"}]})

    assert [d["id"] for d in итог["deltas"]] == ["a", "b"]
    assert [d["id"] for d in хранилище.get(S.METHODOLOGY, "7", "overlay")["deltas"]] == ["a", "b"]


def test_mutate_on_an_empty_place_gets_none(хранилище):
    """Первая правка — документа ещё нет, и изменяющая функция это видит."""
    итог = хранилище.mutate(S.METHODOLOGY, "7", "overlay",
                            lambda d: {"было": d, "deltas": []})

    assert итог["было"] is None


def test_a_raising_mutation_leaves_the_document_untouched(хранилище):
    """🔴 ПРОВЕРКА ДО ЗАПИСИ. Кривая правка не должна лечь в хранилище.

    Личная методика применяет дельту к базе ВНУТРИ изменения и падает там, если
    правка не применяется. Если бы сломанное изменение оставляло след, одна
    кривая правка ломала бы все последующие анализы пользователя.
    """
    хранилище.put(S.METHODOLOGY, "7", "overlay", {"deltas": [{"id": "a"}]})

    def _падает(_):
        raise ValueError("правка не применяется")

    with pytest.raises(ValueError):
        хранилище.mutate(S.METHODOLOGY, "7", "overlay", _падает)

    assert хранилище.get(S.METHODOLOGY, "7", "overlay") == {"deltas": [{"id": "a"}]}


def test_two_edits_at_once_do_not_lose_one_another(хранилище):
    """🔴 РАДИ ЭТОГО ПЕРЕЕЗД И ДЕЛАЛСЯ (#148).

    Две правки, пришедшие одновременно, читают один документ, и вторая пишет
    поверх первой: правка пропадает БЕЗ ошибки — отказ, неотличимый от «не
    сработало». На томе это закрывал файловый замок, чьё описание прямо
    обещало: «снимется переездом в Postgres — там это транзакция».

    ⚠️ ЧЕСТНО О ГРАНИЦАХ ЭТОЙ ПРОВЕРКИ. Настоящую гонку двух ПРОЦЕССОВ тест в
    одном процессе не воспроизводит. Здесь проверяется наблюдаемое следствие:
    два последовательных изменения складываются, а не затирают друг друга, —
    то есть `mutate` действительно читает СВЕЖЕЕ значение, а не то, что
    вызывающий держал в руках. Взаимное исключение под нагрузкой этим не
    доказано, и записать обратное значило бы соврать зелёным тестом.
    """
    хранилище.put(S.METHODOLOGY, "7", "overlay", {"deltas": []})
    устаревший_снимок = хранилище.get(S.METHODOLOGY, "7", "overlay")

    хранилище.mutate(S.METHODOLOGY, "7", "overlay",
                     lambda d: {"deltas": [*d["deltas"], {"id": "первая"}]})
    # Вторая правка держит СТАРЫЙ снимок — и всё равно не должна потерять первую.
    хранилище.mutate(S.METHODOLOGY, "7", "overlay",
                     lambda d: {"deltas": [*d["deltas"], {"id": "вторая"}]})

    assert устаревший_снимок == {"deltas": []}
    assert [d["id"] for d in хранилище.get(S.METHODOLOGY, "7", "overlay")["deltas"]] == [
        "первая", "вторая"]


def test_the_switch_defaults_to_files(бд, tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "state_database_url", "")
    S._cached = None
    assert isinstance(S.get_state_store(), S.LocalStateStore)

    monkeypatch.setattr(settings, "state_database_url", бд)
    S._cached = None
    assert isinstance(S.get_state_store(), S.SqlStateStore)
    S._cached = None


def test_the_segment_is_built_the_same_way_for_both():
    assert S.safe_segment(7) == "7"
    assert "/" not in S.safe_segment("../../etc")


# ─────────── ПЕРЕЕЗД СТАРЫХ ФАЙЛОВ В НОВУЮ РАСКЛАДКУ ───────────


def test_the_migration_finds_the_old_layout(бд, tmp_path):
    """🔴 СТОРОЖ НА МОЮ СОБСТВЕННУЮ ЗАБЫВЧИВОСТЬ, И ОНА ХАРАКТЕРНА.

    Модули перевели на общее состояние, тумблер включили — а раскладка у нового
    хранилища СВОЯ (`<вид>/<владелец>/<ключ>.json`), и старые файлы под неё не
    попадают. Отказа при этом нет ни одного: список наблюдения молча
    пересевается из встроенного реестра, методика читается пустой, правка
    каталога новостей сбрасывается. Пользователь видит не ошибку, а «настройки
    сбросились» — та самая недоставка, которую мы ловим весь месяц.

    Мутация: убрать `projects/<pid>/watchlist.json` из списка предметов — тест
    краснеет на списке наблюдения. Проверено.
    """
    from finengine.store.migrate_user_state import migrate

    корень = tmp_path / "users"
    (корень / "7" / "projects" / "проект-1").mkdir(parents=True)
    (корень / "7" / "sources.json").write_text('{"disabled": ["rbc"]}', encoding="utf-8")
    (корень / "7" / "methodology.json").write_text(
        '{"version": 1, "deltas": [{"id": "bank.formula.nim"}]}', encoding="utf-8")
    (корень / "7" / "projects" / "проект-1" / "watchlist.json").write_text(
        '{"ru": ["Сбер"], "world": []}', encoding="utf-8")
    (корень / "8").mkdir()
    (корень / "8" / "sources.json").write_text("{не json", encoding="utf-8")

    store = S.SqlStateStore(бд)
    отчёт = migrate(корень, store)

    assert отчёт == {"found": 4, "migrated": 3, "already": 0, "unreadable": 1}
    assert store.get(S.USER_STATE, "7", "sources") == {"disabled": ["rbc"]}
    assert store.get(S.METHODOLOGY, "7", "overlay")["deltas"][0]["id"] == "bank.formula.nim"
    assert store.get(S.WATCHLIST, "7", "проект-1") == {"ru": ["Сбер"], "world": []}
    # Чужой владелец их не видит — граница держится и после переезда.
    assert store.get(S.WATCHLIST, "8", "проект-1") is None

    # Повторный запуск не откатывает правки, сделанные уже после переезда.
    store.put(S.WATCHLIST, "7", "проект-1", {"ru": ["Сбер", "ВТБ"], "world": []})
    assert migrate(корень, store)["already"] == 3
    assert store.get(S.WATCHLIST, "7", "проект-1")["ru"] == ["Сбер", "ВТБ"]


def test_the_migrated_watchlist_is_what_the_module_reads(бд, tmp_path, monkeypatch):
    """Переезд проверяется ЖИВЫМ путём, а не только содержимым таблицы.

    Сверка ключей в тесте переезда доказывает, что строка легла. Но упасть
    можно и дальше: модуль строит ключ проекта своей функцией, и разойдись она
    с переездом — таблица была бы полна, а список наблюдения пуст.
    """
    from finengine.config import settings
    from finengine.skills import watchlist
    from finengine.store.migrate_user_state import migrate

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "state_database_url", бд)
    S._cached = None

    корень = tmp_path / "users"
    (корень / "7" / "projects" / "проект-1").mkdir(parents=True)
    (корень / "7" / "projects" / "проект-1" / "watchlist.json").write_text(
        '{"ru": ["Сбер"], "world": ["Apple"]}', encoding="utf-8")

    migrate(корень, S.get_state_store())

    assert watchlist.load("7", "проект-1") == {"ru": ["Сбер"], "world": ["Apple"]}
    S._cached = None
