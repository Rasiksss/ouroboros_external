"""Сценарии: тот же файл читается как есть, и SQL больше не собирается строкой.

🔴 ЗАЧЕМ (решение владельца 25.08.2026). Модуль был написан на голом `sqlite3`
с `PRAGMA`, `sqlite_master`, `executescript` и `INSERT OR REPLACE` — всё это
есть ТОЛЬКО у SQLite, и на Postgres он не запускался в принципе. Нарушение
`docs/rules/architecture.md` 2.1 (🔴 MUST) «Postgres — единственная БД…
признак регрессии: файловые БД».

Заодно `update` и `set_status` собирали SQL f-строкой — `SET {', '.join(sets)}`.
Имена колонок брались из белого списка, дырой это не было, но
`docs/rules/security.md` 2.1 (🔴 MUST) требует параметризации.

САМОЕ ВАЖНОЕ СВОЙСТВО ЗДЕСЬ — СОВМЕСТИМОСТЬ. Переписать слой доступа к живому
файлу и не суметь его прочитать значит потерять автоматизации пользователей.
Поэтому первый тест — не про Postgres, а про то, что СТАРЫЙ ФАЙЛ читается
новым кодом без переноса.
"""
from __future__ import annotations

import sqlite3

import pytest

import finengine.skills.scenarios.store as store
from finengine.config import settings
from finengine.store.migrate_scenarios import migrate

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



@pytest.fixture
def свежая(бд_хранилищ, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store._engines.clear()
    store.init_schema()
    yield tmp_path
    store._engines.clear()


def _старый_файл(путь) -> None:
    """Файл РОВНО такой, какой делал прежний код на голом sqlite3."""
    conn = sqlite3.connect(str(путь), isolation_level=None)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE scenarios (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '',
            freq TEXT NOT NULL DEFAULT 'manual', at_time TEXT NOT NULL DEFAULT '09:00',
            dows TEXT NOT NULL DEFAULT '', paused INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, last_run_at REAL,
            last_status TEXT NOT NULL DEFAULT '', last_run_id TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            params TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO schema_version(version) VALUES (3);
        INSERT INTO scenarios(id, user_id, name, kind, freq, created_at, last_status)
        VALUES ('стар-1', 1, 'IR Daily', 'ir_daily', 'daily', 1000.0, 'ok');
    """)
    conn.close()


def test_the_old_file_arrives_whole_through_the_migration(бд_хранилищ, tmp_path, monkeypatch):
    """🔴 ГЛАВНОЕ: накопленный файл доезжает ЦЕЛИКОМ и читается новым кодом.

    Переписать слой доступа и не суметь прочитать накопленное значит потерять
    автоматизации пользователей. Схема файла объявлена так, чтобы совпасть с
    той, что накопили миграции v1–v3.

    ⚠️ 26.08.2026 тест изменил смысл. Раньше он проверял чтение файла НАПРЯМУЮ:
    пустая настройка открывала `scenarios.db`. Запасной путь снесён — модуль
    знает только базу, и файл стал наследием, из которого данные приезжают
    переездом. Проверяемое обещание то же: накопленное не теряется.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _старый_файл(tmp_path / "scenarios.db")
    store._engines.clear()
    migrate(f"sqlite:///{tmp_path / 'scenarios.db'}", бд_хранилищ)

    сцены = store.list_for_user(1)

    assert [s.name for s in сцены] == ["IR Daily"]
    assert сцены[0].kind == "ir_daily" and сцены[0].last_status == "ok"


def test_init_schema_after_the_migration_is_harmless(бд_хранилищ, tmp_path, monkeypatch):
    """`init_schema` зовётся на каждом старте — на перевезённой базе он безвреден.

    ⚠️ 26.08.2026: раньше проверка шла на СТАРОМ ФАЙЛЕ, который модуль открывал
    сам. Файлового пути нет; свойство осталось прежним — повторный `init_schema`
    не ломает накопленное, — и проверяется там, где накопленное теперь лежит.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _старый_файл(tmp_path / "scenarios.db")
    store._engines.clear()
    migrate(f"sqlite:///{tmp_path / 'scenarios.db'}", бд_хранилищ)

    store.init_schema()
    store.init_schema()  # идемпотентность

    assert len(store.list_for_user(1)) == 1


def test_crud_round_trip(свежая):
    s = store.create(user_id=7, name="проба", kind="ir_daily", freq="daily",
                     dows=[1, 3, 1])
    assert s.dows == "1,3", "дни недели не нормализованы"
    assert store.get(s.id).name == "проба"
    assert [x.id for x in store.list_for_user(7)] == [s.id]
    assert [x.id for x in store.list_all_active()] == [s.id]

    store.update(s.id, name="иначе", paused=True)
    после = store.get(s.id)
    assert после.name == "иначе" and после.paused == 1
    assert store.list_all_active() == [], "на паузе — не активен"

    assert store.delete(s.id) is True
    assert store.get(s.id) is None


def test_update_ignores_unknown_and_none(свежая):
    """Белый список полей сохранён: чужой ключ не должен доехать до SQL."""
    s = store.create(user_id=7, name="проба", kind="ir_daily")
    store.update(s.id, name=None, чужое_поле="вредное", kind="ir_calendar")
    после = store.get(s.id)
    assert после.name == "проба", "None затёр значение"
    assert после.kind == "ir_calendar"


def test_sweep_marks_stale_running_as_error(свежая):
    """Прогоны живут в процессе API: после рестарта любой 'running' мёртв."""
    s = store.create(user_id=7, name="проба", kind="ir_daily")
    store.set_status(s.id, last_status="running")
    assert store.sweep_stale_running() == 1
    после = store.get(s.id)
    assert после.last_status == "error"
    assert "перезапуском" in после.last_error


def test_no_sql_is_built_by_string_formatting():
    """🔴 security.md 2.1: SQL не собирается f-строкой.

    Ловим форму: `f"...SET ..."` и конкатенацию имён колонок. Это грубая
    проверка, и она сказана как есть — она не докажет отсутствие инъекции,
    но ловит именно тот приём, который отсюда убрали.
    """
    from pathlib import Path
    import re

    исходник = (Path(__file__).resolve().parents[1] / "src" / "finengine"
                / "skills" / "scenarios" / "store.py").read_text(encoding="utf-8")
    подозрительные = [
        s for s in исходник.splitlines()
        if re.search(r'f"[^"]*(SELECT|INSERT|UPDATE|DELETE|SET)\b', s, re.I)
        and not s.lstrip().startswith("#")
    ]
    assert not подозрительные, (
        f"SQL снова собирается f-строкой: {подозрительные}")


def test_no_sqlite_only_constructs_remain():
    """`PRAGMA`, `sqlite_master`, `executescript` есть только у SQLite.

    Оставь их — и модуль не запустится на Postgres вовсе, то есть правило
    «Postgres — единственная БД» останется невыполнимым технически.

    🔴 ДВЕ ПРЕДЫДУЩИЕ РЕДАКЦИИ ЭТОГО ТЕСТА БЫЛИ НЕГОДНЫМИ, и обе поймал
    отрицательный контроль:

      1. фильтр по виду строчки (`#` в начале, три кавычки внутри) краснел на
         СОБСТВЕННОМ докстринге модуля, где эти конструкции перечислены как
         убранные;
      2. токенизация с отбросом всех STRING-токенов не видела PRAGMA ВНУТРИ
         строки — а SQL только там и живёт. Мутация «вернуть PRAGMA через
         text()» проходила зелёной, то есть тест не сторожил ничего.

    Верный способ — AST: отбрасываем ДОКСТРИНГИ (текст О коде) и смотрим все
    остальные строковые литералы, потому что SQL пишут именно в них.
    """
    import ast
    from pathlib import Path

    путь = (Path(__file__).resolve().parents[1] / "src" / "finengine"
            / "skills" / "scenarios" / "store.py")
    дерево = ast.parse(путь.read_text(encoding="utf-8"))

    докстринги: set[int] = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            телo = getattr(узел, "body", None)
            if телo and isinstance(телo[0], ast.Expr) and isinstance(телo[0].value, ast.Constant) \
               and isinstance(телo[0].value.value, str):
                докстринги.add(id(телo[0].value))

    литералы = [n.value for n in ast.walk(дерево)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in докстринги]
    текст = " ".join(литералы)
    for конструкция in ("PRAGMA", "sqlite_master", "executescript", "INSERT OR REPLACE"):
        assert конструкция not in текст, (
            f"в КОДЕ осталась SQLite-только конструкция {конструкция!r}")

    # И сам модуль sqlite3 больше не импортируется — на Postgres он не нужен.
    импорты = {a.name for n in ast.walk(дерево) if isinstance(n, ast.Import) for a in n.names}
    assert "sqlite3" not in импорты, "модуль снова импортирует sqlite3 напрямую"


def test_migration_is_additive(tmp_path, monkeypatch):
    """Повтор переезда не откатывает правку, сделанную уже на базе."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _старый_файл(tmp_path / "scenarios.db")
    цель = f"sqlite:///{tmp_path / 'target.db'}"

    о = migrate(f"sqlite:///{tmp_path / 'scenarios.db'}", цель)
    assert о["migrated"] == 1 and о["already"] == 0

    # на базе поработали
    monkeypatch.setattr(settings, "scenarios_database_url", цель)
    store._engines.clear()
    store.update("стар-1", name="переименован уже на базе")

    о2 = migrate(f"sqlite:///{tmp_path / 'scenarios.db'}", цель)

    assert о2["migrated"] == 0 and о2["already"] == 1
    assert store.get("стар-1").name == "переименован уже на базе"
    store._engines.clear()


def test_a_missing_address_is_a_loud_refusal_not_a_quiet_file(monkeypatch):
    """🔴 ФАЙЛОВОГО УМОЛЧАНИЯ БОЛЬШЕ НЕТ (26.08.2026).

    Здесь проверялось обратное: пустая настройка = тот же файл, что и раньше.
    Пока так было, ненастроенный контур работал «как будто получилось» — писал
    в базу сценариев, которую никто не читает. Мутация: вернуть открытие файла
    вместо отказа — тест краснеет.
    """
    from finengine.config import settings as s

    monkeypatch.setattr(s, "scenarios_database_url", "")
    with pytest.raises(RuntimeError, match="SCENARIOS_DATABASE_URL"):
        store.engine()
