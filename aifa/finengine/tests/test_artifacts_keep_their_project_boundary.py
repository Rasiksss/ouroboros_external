"""Артефакты: одно поведение на обе реализации, чужой проект не отдаётся.

🔴 ЗАЧЕМ. При переезде артефактов с тома в базу граница между пользователями и
проектами меняет природу так же тихо, как у сессий: была каталогом
(`artifacts/<user>/<project>/<run>/`), стала парой условий в запросе. Разбор
цены ошибки — в шапке `store/sessions.py` и в
`tests/test_sessions_keep_their_owner_boundary.py`.

Проверки идут на ОБЕИХ реализациях одним набором: расхождение здесь дефект, а
не особенность бэкенда. У сессий ровно такой набор сразу поймал разошедшееся
поведение первичного ключа — поэтому он повторён и тут.
"""
from __future__ import annotations

import pytest

from finengine.store import artifacts as A


@pytest.fixture(params=["файлы", "база"])
def хранилище(request, tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    if request.param == "файлы":
        return A.LocalArtifactStore()
    # Базу спрашиваем ЗДЕСЬ, а не в сигнатуре: вариант «файлы» её не
    # требует, и общий запрос пропускал бы файловую ветку без причины.
    return A.SqlArtifactStore(request.getfixturevalue('бд'))


def _карточка(run_id: str, *, created_at: float = 100.0, kind: str = "ir_calendar"):
    return A.ArtifactMeta(run_id=run_id, kind=kind, created_at=created_at,
                          title="Календарь", summary="47 событий")


def test_an_artifact_survives_a_round_trip(хранилище):
    хранилище.save("7", "проект-1", _карточка("r1"), {"events": [1, 2]})

    итог = хранилище.load("7", "проект-1", "r1")
    assert итог["payload"] == {"events": [1, 2]}
    assert итог["meta"]["title"] == "Календарь"


def test_another_user_cannot_read_the_artifact(хранилище):
    """🔴 ГЛАВНОЕ СВОЙСТВО. Мутация: убрать `owner_key` из условия — краснеет."""
    хранилище.save("7", "проект-1", _карточка("r1"), {"x": 1})

    assert хранилище.load("8", "проект-1", "r1") is None
    assert хранилище.list("8", "проект-1") == []


def test_another_project_of_the_same_user_is_separate(хранилище):
    """Проект — тоже граница: у одного пользователя их много."""
    хранилище.save("7", "проект-1", _карточка("r1"), {"x": 1})

    assert хранилище.load("7", "проект-2", "r1") is None
    assert хранилище.list("7", "проект-2") == []


def test_another_user_cannot_delete_the_artifact(хранилище):
    хранилище.save("7", "проект-1", _карточка("r1"), {"x": 1})

    assert хранилище.delete("8", "проект-1", "r1") is False
    assert хранилище.load("7", "проект-1", "r1") is not None


def test_the_list_is_newest_first(хранилище):
    хранилище.save("7", "п", _карточка("старый", created_at=100.0), {})
    хранилище.save("7", "п", _карточка("новый", created_at=200.0), {})

    assert [m.run_id for m in хранилище.list("7", "п")] == ["новый", "старый"]


def test_saving_the_same_run_twice_replaces_it(хранилище):
    хранилище.save("7", "п", _карточка("r1"), {"версия": 1})
    хранилище.save("7", "п", _карточка("r1"), {"версия": 2})

    assert len(хранилище.list("7", "п")) == 1
    assert хранилище.load("7", "п", "r1")["payload"] == {"версия": 2}


def test_deleting_says_whether_there_was_anything(хранилище):
    assert хранилище.delete("7", "п", "нет-такого") is False
    хранилище.save("7", "п", _карточка("r1"), {})
    assert хранилище.delete("7", "п", "r1") is True
    assert хранилище.load("7", "п", "r1") is None


def test_a_missing_artifact_raises_for_the_caller(tmp_path, monkeypatch):
    """Открытый вызов сохранил прежний договор: нет артефакта — исключение.

    Девять потребителей писались под `FileNotFoundError`; смена на `None`
    прошла бы мимо тестов хранилища и всплыла бы у пользователя пустой
    страницей вместо честного 404.
    """
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "artifacts_database_url", "")
    A._cached = None
    with pytest.raises(FileNotFoundError):
        A.load_artifact(7, "п", "нет-такого")
    A._cached = None


def test_the_switch_defaults_to_files(бд, tmp_path, monkeypatch):
    """Пусто в настройке = как было. Код ничего не включает сам."""
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "artifacts_database_url", "")
    A._cached = None
    assert isinstance(A.get_artifact_store(), A.LocalArtifactStore)

    monkeypatch.setattr(settings, "artifacts_database_url", бд)
    A._cached = None
    assert isinstance(A.get_artifact_store(), A.SqlArtifactStore)
    A._cached = None


def test_the_migration_keeps_owner_and_project_from_the_path(бд, tmp_path):
    """Переезд: владелец и проект берутся из пути — в карточке их нет вовсе."""
    import json

    from finengine.store.migrate_artifacts import migrate

    прогон = tmp_path / "artifacts" / "7" / "проект-1" / "r1"
    прогон.mkdir(parents=True)
    (прогон / "meta.json").write_text(
        json.dumps(_карточка("r1").to_dict(), ensure_ascii=False), encoding="utf-8")
    (прогон / "result.json").write_text('{"events": [1]}', encoding="utf-8")
    битый = tmp_path / "artifacts" / "7" / "проект-1" / "r2"
    битый.mkdir(parents=True)
    (битый / "meta.json").write_text("{не json", encoding="utf-8")

    цель = A.SqlArtifactStore(бд)
    отчёт = migrate(tmp_path / "artifacts", цель)

    assert отчёт == {"on_disk": 2, "migrated": 1, "already": 0, "unreadable": 1}
    assert цель.load("7", "проект-1", "r1")["payload"] == {"events": [1]}
    assert цель.load("8", "проект-1", "r1") is None
    assert migrate(tmp_path / "artifacts", цель)["already"] == 1
