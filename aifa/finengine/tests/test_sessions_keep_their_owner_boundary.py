"""Сессии: одно поведение на обе реализации, и чужую не отдаём ни одной.

🔴 ЗАЧЕМ ЭТОТ ФАЙЛ ВООБЩЕ ЕСТЬ. При переезде сессий с тома в базу граница между
пользователями МЕНЯЕТ ПРИРОДУ, и это тихо. Была физическая: сессии лежали в
`data/sessions/<owner_id>/`, и чужой `session_id` в каталоге владельца просто
не находился — так и было написано в комментарии роута восстановления треда:
«cross-user load is physically impossible». В базе стены нет: все сессии в одной
таблице, и разделяет их ровно одно условие `owner_key` в запросе.

Забыть это условие — значит открыть чтение чужих переписок. Ни один тест на
«сессия сохранилась и загрузилась» этого не увидит: свой владелец читает свою
сессию одинаково успешно и с условием, и без него. Поэтому проверка идёт на
ОБЕИХ реализациях одним набором — расхождение здесь дефект, а не особенность
бэкенда (`docs/rules/security.md` 3.1: «проверка владельца ресурса на каждом
защищённом роуте»).
"""
from __future__ import annotations

import pytest

from finengine.store import sessions as S


@pytest.fixture(params=["файлы", "база"])
def хранилище(request, tmp_path, monkeypatch):
    """Одни и те же проверки — и на томе, и в базе."""
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    if request.param == "файлы":
        return S.LocalSessionStore()
    # Базу спрашиваем ЗДЕСЬ, а не в сигнатуре: вариант «файлы» её не
    # требует, и общий запрос пропускал бы файловую ветку без причины.
    return S.SqlSessionStore(request.getfixturevalue('бд'))


ИСТОРИЯ = {"session_id": "s-1", "history": [{"role": "user", "content": "выручка?"}]}


def test_a_session_survives_a_round_trip(хранилище):
    хранилище.save(7, "s-1", ИСТОРИЯ)

    assert хранилище.load(7, "s-1") == ИСТОРИЯ


def test_another_user_cannot_read_the_session(хранилище):
    """🔴 ГЛАВНОЕ СВОЙСТВО. Чужой `session_id` не отдаётся — обеими ветками.

    Мутация, которая это краснит: убрать `owner_key` из условия `load`
    в `SqlSessionStore`. Проверено — тест падает на ветке «база» и остаётся
    зелёным на «файлах», то есть различает именно новую реализацию.
    """
    хранилище.save(7, "s-1", ИСТОРИЯ)

    assert хранилище.load(8, "s-1") is None
    assert хранилище.load(None, "s-1") is None


def test_another_user_cannot_overwrite_the_session(хранилище):
    """Запись тоже под владельцем: иначе чужой ход затрёт чужую историю.

    Мутация: убрать `owner_key` из условия в `save` — тест краснеет на «базе».
    """
    хранилище.save(7, "s-1", ИСТОРИЯ)
    хранилище.save(8, "s-1", {"session_id": "s-1", "history": [{"role": "user",
                                                                "content": "чужое"}]})

    assert хранилище.load(7, "s-1") == ИСТОРИЯ


def test_a_missing_session_is_none_not_a_crash(хранилище):
    assert хранилище.load(7, "нет-такой") is None


def test_saving_twice_replaces_rather_than_duplicates(хранилище):
    хранилище.save(7, "s-1", ИСТОРИЯ)
    хранилище.save(7, "s-1", {"session_id": "s-1", "history": [{"role": "user",
                                                                "content": "второй ход"}]})

    загружено = хранилище.load(7, "s-1")
    assert загружено["history"][0]["content"] == "второй ход"
    assert len(загружено["history"]) == 1


def test_the_owner_key_is_built_the_same_way_for_both(tmp_path, monkeypatch):
    """Ключ владельца собирается одной функцией — иначе история «пропадёт».

    Если файлы кладут под `7`, а база ищет под `user-7`, после переезда
    пользователь увидит пустой тред. Отказа не будет: обе стороны сработают
    штатно, просто разойдутся.
    """
    assert S.owner_key(7) == "7"
    assert S.owner_key(None) == "_global"
    # Набор символов сужен: `owner_id` приходит из запроса и не должен уводить
    # путь за пределы каталога сессий.
    assert "/" not in S.owner_key("../../etc")
    assert ".." not in S.owner_key("../../etc").replace("etc", "")


def test_the_switch_defaults_to_files(бд, tmp_path, monkeypatch):
    """Пусто в настройке = как было. Код ничего не включает сам.

    Тумблер ОДИН на всё именованное состояние (`FINENGINE_STATE_DATABASE_URL`),
    а не свой у каждого вида: иначе их стало бы пять на одно хранилище, и
    включение половины давало бы наполовину переехавшую систему.
    """
    from finengine.config import settings
    from finengine.store import state

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "state_database_url", "")
    state._cached = None
    assert isinstance(S.get_session_store()._store, state.LocalStateStore)

    monkeypatch.setattr(settings, "state_database_url", бд)
    state._cached = None
    assert isinstance(S.get_session_store()._store, state.SqlStateStore)
    state._cached = None


def test_the_migration_keeps_each_session_with_its_owner(бд, tmp_path):
    """Переезд: владелец берётся из каталога, а не из содержимого файла.

    Мутация: брать владельца из поля внутри JSON — тест краснеет на сессии,
    у которой этого поля нет (старые сессии его не писали), потому что она
    уехала бы к `_global` и пропала бы из треда пользователя.
    """
    from finengine.store.migrate_sessions import migrate

    корень = tmp_path / "sessions"
    (корень / "7").mkdir(parents=True)
    (корень / "8").mkdir(parents=True)
    # У этой сессии владельца внутри НЕТ — как у старых.
    (корень / "7" / "s-1.json").write_text('{"history": [{"role": "user", "content": "моё"}]}',
                                           encoding="utf-8")
    (корень / "8" / "s-2.json").write_text('{"owner_id": 8, "history": []}', encoding="utf-8")
    (корень / "8" / "битая.json").write_text("{не json", encoding="utf-8")

    цель = S.SqlSessionStore(бд)
    отчёт = migrate(корень, цель)

    assert отчёт == {"on_disk": 3, "migrated": 2, "already": 0, "unreadable": 1}
    assert цель.load(7, "s-1")["history"][0]["content"] == "моё"
    assert цель.load(None, "s-1") is None
    assert цель.load(8, "s-2") is not None

    # Повторный запуск ничего не переносит заново — иначе он откатывал бы
    # ходы, сделанные уже на базе.
    assert migrate(корень, цель)["already"] == 2
