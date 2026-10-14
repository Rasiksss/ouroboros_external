"""Уборка тома: удаляем только то, чья копия подтверждена ПРЯМО СЕЙЧАС.

🔴 ЗАЧЕМ СТОРОЖ НА УДАЛЕНИЕ. Оно необратимо, и ошибиться здесь можно ровно
одним способом: удалить по списку «что мы вроде бы перенесли» вместо проверки.
На стенде это стоило бы семи отчётов — PDF, которых нет ни в реестре, ни в
хранилище: они выглядят как остальные и попали бы под общую метлу.

Поэтому проверяется не «сколько удалилось», а РАЗЛИЧЕНИЕ: рядом кладутся
предмет с копией и предмет без копии, и уборка обязана взять только первый.
"""
from __future__ import annotations


import pytest

from finengine.store import blobs, state
from finengine.store.cleanup_volume import cleanup

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



@pytest.fixture
def том(tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path / "documents")
    monkeypatch.setattr(settings, "s3_endpoint", "")
    monkeypatch.setattr(settings, "state_database_url", f"sqlite:///{tmp_path / 'st.db'}")
    monkeypatch.setattr(settings, "journals_database_url", "")
    (tmp_path / "documents").mkdir(parents=True)
    from finengine.store import journals as _journals

    blobs._cached = None
    state._cached = None
    _journals._cached = None
    yield tmp_path
    blobs._cached = None
    state._cached = None
    _journals._cached = None


def test_a_sidecar_without_a_copy_survives(том):
    """🔴 ГЛАВНОЕ РАЗЛИЧЕНИЕ. Мутация: удалять текст, не спрашивая хранилище,
    — тест краснеет на файле «без-копии»."""
    (том / "documents" / "с-копией.txt").write_text("текст", encoding="utf-8")
    (том / "documents" / "без-копии.txt").write_text("текст", encoding="utf-8")
    blobs.save_document_text("с-копией", "текст")

    у = cleanup(том, dry_run=False)

    assert not (том / "documents" / "с-копией.txt").exists()
    assert (том / "documents" / "без-копии.txt").exists(), (
        "удалён единственный экземпляр — это не уборка")
    assert any("без-копии" in с for с in у.оставлено)


def test_an_orphan_pdf_survives(том):
    """PDF-сирота (нет ни в реестре, ни в хранилище) не удаляется."""
    (том / "documents" / "d1_отчёт.pdf").write_bytes(b"%PDF-1.7")
    (том / "documents" / "сирота_отчёт.pdf").write_bytes(b"%PDF-1.7")
    blobs.get_blob_store().put("documents/d1/d1_отчёт.pdf", b"%PDF-1.7")

    cleanup(том, dry_run=False)

    assert not (том / "documents" / "d1_отчёт.pdf").exists()
    assert (том / "documents" / "сирота_отчёт.pdf").exists()


def test_a_journal_that_did_not_migrate_survives(том):
    """🔴 ЖУРНАЛ БЕЗ КОПИИ В ХРАНИЛИЩЕ ОСТАЁТСЯ.

    Журналы прогонов переехали 26.08, но проверяются ПОШТУЧНО: файл, чья копия
    не подтверждена, удалять нельзя. Списком «что мы вроде бы перенесли» здесь
    пользоваться нельзя — ровно так теряют единственный экземпляр.

    ⚠️ Прежде этот тест утверждал, что журналы не трогают ВООБЩЕ («ещё не
    переезжали»). После переезда это перестало быть правдой, и охранять надо
    другое: не «никогда», а «только с подтверждённой копией».

    Мутация: удалять журналы, не спрашивая хранилище, — тест краснеет.
    """
    (том / "documents" / "extract_runs.jsonl").write_text('{"ok":1}\n', encoding="utf-8")
    прогоны = том / "documents" / "extract_events"
    прогоны.mkdir()
    (прогоны / "r1.jsonl").write_text('{"type":"done"}\n', encoding="utf-8")
    (том / "documents" / "чужая.bak-before-smoke").write_text("x", encoding="utf-8")

    cleanup(том, dry_run=False)

    assert (том / "documents" / "extract_runs.jsonl").exists(), "журнал без копии удалён"
    assert (прогоны / "r1.jsonl").exists(), "журнал прогона без копии удалён"
    assert (том / "documents" / "чужая.bak-before-smoke").exists(), "снят чужой снимок"


def test_a_migrated_journal_is_removed(том):
    """А журнал, чья копия подтверждена, уходит — ради этого уборка и есть."""
    from finengine.skills import run_logger as rl
    from finengine.skills.run_orchestrator import вид_журнала
    from finengine.store import journals

    (том / "documents" / "extract_runs.jsonl").write_text('{"ok":1}\n', encoding="utf-8")
    journals.append(rl.ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, owner="_", key=rl._ОБЩИЙ, entry={"ok": 1})

    прогоны = том / "documents" / "extract_events"
    прогоны.mkdir()
    (прогоны / "r1.jsonl").write_text('{"type":"done"}\n', encoding="utf-8")
    journals.append(вид_журнала("extract"), owner="_", key="r1", entry={"type": "done"})

    cleanup(том, dry_run=False)

    assert not (том / "documents" / "extract_runs.jsonl").exists()
    assert not прогоны.exists()


def test_the_dry_run_deletes_nothing(том):
    """Сухой прогон печатает то же, что удалит настоящий, но не удаляет."""
    (том / "documents" / "с-копией.txt").write_text("текст", encoding="utf-8")
    blobs.save_document_text("с-копией", "текст")

    у = cleanup(том, dry_run=True)

    assert у.удалено == 1
    assert (том / "documents" / "с-копией.txt").exists()


def test_user_state_without_a_row_survives(том):
    """Список наблюдения без строки в базе остаётся файлом.

    Это тот же класс, что забытый переезд личного состояния: переключатель
    включили, переезд не запустили — и уборка стёрла бы настройки, которые
    никуда не переехали.
    """
    проект = том / "users" / "1" / "projects" / "переехал"
    проект.mkdir(parents=True)
    (проект / "watchlist.json").write_text('{"ru": ["Сбер"]}', encoding="utf-8")
    не_переехал = том / "users" / "1" / "projects" / "не-переехал"
    не_переехал.mkdir(parents=True)
    (не_переехал / "watchlist.json").write_text('{"ru": ["ВТБ"]}', encoding="utf-8")
    state.put(state.WATCHLIST, owner="1", key="переехал", doc={"ru": ["Сбер"]})

    cleanup(том, dry_run=False)

    assert not (проект / "watchlist.json").exists()
    assert (не_переехал / "watchlist.json").exists()


def test_chat_knowledge_is_left_alone(том):
    """Знания чата на общее состояние не переведены — под уборку не идут."""
    знания = том / "users" / "1" / "chats" / "c1" / "knowledge"
    знания.mkdir(parents=True)
    (знания / "sources.json").write_text('{"a": 1}', encoding="utf-8")
    (знания / "chunks.jsonl").write_text('{"t":"x"}\n', encoding="utf-8")

    cleanup(том, dry_run=False)

    assert (знания / "sources.json").exists()
    assert (знания / "chunks.jsonl").exists()


def test_a_file_database_with_more_rows_than_the_base_survives(том, monkeypatch):
    """Файловая база, где строк БОЛЬШЕ, чем в базе, не удаляется.

    Это признак недовершённого переезда, и стереть файл значило бы потерять
    разницу. Мутация: сравнивать на равенство «есть таблица — удаляем» — тест
    краснеет.
    """
    import sqlite3

    import sqlalchemy as sa

    from finengine.config import settings

    файл = том / "scenarios.db"
    c = sqlite3.connect(файл)
    c.execute("create table scenarios (id text)")
    c.executemany("insert into scenarios values (?)", [("a",), ("b",), ("c",)])
    c.commit()
    # В ЕЁ базе (у сценариев свой адрес) заводим ту же таблицу, но с одной
    # строкой из трёх — то есть переезд не довершён.
    # ⚠️ ЧЕРЕЗ monkeypatch, А НЕ ПРИСВОЕНИЕМ. Первая редакция присваивала
    # настройку напрямую, и адрес утекал в соседние тесты — пять чужих упало.
    адрес = f"sqlite:///{том / 'цель.db'}"
    monkeypatch.setattr(settings, "scenarios_database_url", адрес)
    движок = sa.create_engine(адрес, future=True)
    with движок.begin() as conn:
        conn.execute(sa.text("create table if not exists scenarios (id text)"))
        conn.execute(sa.text("insert into scenarios values ('a')"))

    у = cleanup(том, dry_run=False)

    assert файл.exists(), "стёрта файловая база, где строк больше, чем в базе"
    assert any("scenarios" in с for с in у.оставлено)

