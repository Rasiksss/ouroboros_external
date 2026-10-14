"""Реестр документов в базе, файлы — в объектном хранилище.

🔴 ЗАЧЕМ (решение владельца 25.08.2026: «делай всё, переноси всё»). Реестр жил
в `data/index/meta.pkl`. Три нарушения `docs/rules/architecture.md`, все MUST:

  * 2.1 «Postgres — единственная БД… признак регрессии: файловые БД»;
  * 3.1 «Нет состояния на файловой системе контейнера»;
  * 3.2 «in-process синглтоны… разъезжающиеся на >1 реплике — баги». В коде
    прямо: «ОДИН экземпляр хранилища на процесс», а рядом с загрузкой стоит
    замок с подписью «add_document() is not thread-safe». Не стреляло ТОЛЬКО
    из-за `--workers 1`.

Здесь проверяется не «переезд состоялся», а свойства, без которых переезд
опаснее, чем его отсутствие.
"""
from __future__ import annotations


import pytest

from finengine.models.document import Chunk, DocumentMeta
from finengine.store.blobs import LocalBlobStore
from finengine.store.documents import SqlDocumentStore
from finengine.store.migrate_documents import ПиклРеестр, _ключ, migrate


@pytest.fixture
def база(бд, tmp_path) -> SqlDocumentStore:
    return SqlDocumentStore(бд)


@pytest.fixture
def объекты(tmp_path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def _пикл(tmp_path, документы: list[DocumentMeta], chunks=()) -> ПиклРеестр:
    """Собрать СТАРЫЙ файловый реестр — ровно такой, какой оставил боевой код.

    Пишем pickle напрямую: боевого класса поверх `meta.pkl` больше нет (снесён
    26.08.2026 вместе с прочими файловыми запасными путями). Это тот же приём,
    что у тестов новостей и сценариев, — легаси-артефакт строит сам тест.
    """
    import pickle

    каталог = tmp_path / "index"
    каталог.mkdir(parents=True, exist_ok=True)
    with open(каталог / "meta.pkl", "wb") as f:
        pickle.dump({"docs": {m.doc_id: m for m in документы},
                     "chunks": list(chunks)}, f)
    return ПиклРеестр(каталог)


def _док(doc_id="d1", owner=7, имя="отчёт.pdf", путь="") -> DocumentMeta:
    return DocumentMeta(doc_id=doc_id, filename=имя, owner_id=owner,
                        company_name="Северсталь", period="Q1 2025", file_path=путь)


# ── контракт хранилища: он обязан совпасть с файловым ───────────────────────

def test_the_sql_store_keeps_the_same_contract(база):
    """Шесть потребителей не должны знать, что под ними поменялось."""
    m = _док()
    база.add_document(m, [])
    assert база.total_documents == 1
    assert база.get_document("d1").company_name == "Северсталь"
    assert [d.doc_id for d in база.list_documents()] == ["d1"]

    m2 = m.model_copy(update={"company_name": "ММК"})
    база.update_document_meta(m2)
    assert база.get_document("d1").company_name == "ММК"

    база.remove_document("d1")
    assert база.get_document("d1") is None and база.total_documents == 0


def test_adding_the_same_document_twice_updates_it(база):
    """Приём документа идемпотентен: `add_document` зовут и как upsert.

    В `api/main.py` он вызывается трижды по одному пути с комментарием
    «upsert metadata». Вставка вместо обновления дала бы дубль первичного
    ключа и уронила бы загрузку.
    """
    база.add_document(_док(), [])
    база.add_document(_док().model_copy(update={"company_name": "ММК"}), [])
    assert база.total_documents == 1
    assert база.get_document("d1").company_name == "ММК"


def test_update_of_an_unknown_document_does_nothing(база):
    """Сохранено от файловой версии: `update` не должен ЗАВОДИТЬ документ.

    Иначе в списке появился бы документ, которого никто не принимал и файла у
    которого нет.
    """
    база.update_document_meta(_док(doc_id="призрак"))
    assert база.total_documents == 0


def test_owner_is_a_column_not_a_field_inside_a_blob(база):
    """🔴 Владелец — колонка: на нём стоит проверка доступа.

    В пикле `owner_id` лежал ВНУТРИ слепка, и «чей документ» зависело от файла.
    Колонка позволяет спросить это условием запроса.
    """
    from sqlalchemy import select
    from finengine.store.documents import DOCUMENT

    база.add_document(_док(owner=42), [])
    with база._engine.begin() as conn:
        assert conn.execute(select(DOCUMENT.c.owner_id)).scalar() == 42


def test_list_documents_without_owner_returns_everyone(база):
    """`owner_id=None` (по умолчанию) — намеренный режим «все документы»:
    им пользуются миграция и уборка тома, а роут `/documents` отдаёт его
    только админу. Не путать с фильтром — это явный выбор вызывающего,
    а не забытая проверка.
    """
    база.add_document(_док(doc_id="d1", owner=1), [])
    база.add_document(_док(doc_id="d2", owner=2), [])
    база.add_document(_док(doc_id="d3", owner=1), [])

    итог = {d.doc_id for d in база.list_documents()}
    assert итог == {"d1", "d2", "d3"}


def test_list_documents_with_owner_filters_by_the_indexed_column(база):
    """`owner_id=N` фильтрует в самом SQL-запросе (`WHERE owner_id = :N`),
    через ту же индексированную колонку, что и проверка доступа к одному
    документу (см. тест выше) — не через постфильтр в Python.
    """
    база.add_document(_док(doc_id="d1", owner=1), [])
    база.add_document(_док(doc_id="d2", owner=2), [])
    база.add_document(_док(doc_id="d3", owner=1), [])

    assert {d.doc_id for d in база.list_documents(owner_id=1)} == {"d1", "d3"}
    assert {d.doc_id for d in база.list_documents(owner_id=2)} == {"d2"}
    assert база.list_documents(owner_id=999) == []


def test_chunks_survive_even_though_the_live_path_makes_none(база):
    """Куски кладёт только демо-роут — и он не должен сломаться."""
    база.add_document(_док(), [Chunk(doc_id="d1", content="текст", chunk_type="table")])
    assert база.total_chunks == 1
    куски = база.get_chunks_by_type("table", "d1")
    assert len(куски) == 1 and куски[0].content == "текст"
    assert база.get_chunks_by_type("иного-типа", "d1") == []


def test_removing_a_document_takes_its_chunks(база):
    база.add_document(_док(), [Chunk(doc_id="d1", content="т", chunk_type="table")])
    база.remove_document("d1")
    assert база.total_chunks == 0


# ── переезд ─────────────────────────────────────────────────────────────────

def test_migration_moves_the_registry_and_the_file(tmp_path, база, объекты):
    файл = tmp_path / "отчёт.pdf"
    файл.write_bytes(b"%PDF-1.7 ...")
    источник = _пикл(tmp_path, [_док(путь=str(файл))])

    о = migrate(источник, база, объекты)

    assert о["docs_migrated"] == 1 and о["blobs_uploaded"] == 1
    assert объекты.get(_ключ(_док(путь=str(файл)))) == b"%PDF-1.7 ..."


def test_the_record_points_at_the_object_not_at_the_old_path(tmp_path, база, объекты):
    """🔴 Иначе переезд был бы только на бумаге.

    Оставь `file_path` прежним — и код продолжит открывать файл напрямую,
    правило 3.1 останется нарушенным, а объект в хранилище будет мёртвым
    грузом. Прежний путь при этом обязан сохраниться: если что-то пойдёт не
    так, надо видеть, откуда пришли.
    """
    файл = tmp_path / "отчёт.pdf"
    файл.write_bytes(b"x")
    migrate(_пикл(tmp_path, [_док(путь=str(файл))]), база, объекты)

    d = база.get_document("d1")
    assert d.file_path.startswith("documents/"), f"file_path остался прежним: {d.file_path}"
    assert d.meta_card.get("legacy_file_path") == str(файл)


def test_a_second_run_does_not_re_upload_or_overwrite(tmp_path, база, объекты):
    """Идемпотентность: повтор не заливает объект заново и не трогает запись."""
    файл = tmp_path / "отчёт.pdf"
    файл.write_bytes(b"x")
    источник = _пикл(tmp_path, [_док(путь=str(файл))])
    migrate(источник, база, объекты)

    # на базе поработали — имя компании уточнили
    база.update_document_meta(база.get_document("d1").model_copy(
        update={"company_name": "уточнено человеком"}))

    о = migrate(источник, база, объекты)

    assert о["docs_migrated"] == 0 and о["docs_already"] == 1
    assert база.get_document("d1").company_name == "уточнено человеком", (
        "повторный переезд откатил правку, сделанную уже на базе")


def test_a_record_without_a_file_still_reaches_the_registry(tmp_path, база, объекты):
    """Файл пропал, запись есть — метаданные всё равно нужны интерфейсу."""
    о = migrate(_пикл(tmp_path, [_док(путь=str(tmp_path / "нет-такого.pdf"))]),
                база, объекты)
    assert о["files_missing"] == 1 and о["docs_migrated"] == 1
    assert база.get_document("d1") is not None


def test_a_dry_run_writes_nothing(tmp_path, база, объекты):
    файл = tmp_path / "отчёт.pdf"
    файл.write_bytes(b"x")
    о = migrate(_пикл(tmp_path, [_док(путь=str(файл))]), база, объекты, dry_run=True)
    assert о["docs_migrated"] == 1
    assert база.total_documents == 0 and not list(объекты.iter_keys())


def test_a_missing_address_is_a_loud_refusal_not_a_quiet_file():
    """🔴 ЗАПАСНОГО ПУТИ БОЛЬШЕ НЕТ (26.08.2026).

    Пока он был, ненастроенный контур работал «как будто получилось»: писал в
    `data/index/meta.pkl`, которого никто не читает, и пропажа всплывала часом
    позже словами «документ не найден». Мутация: вернуть возврат файлового
    хранилища вместо отказа — тест краснеет.
    """
    import finengine.ingestion.document_store as D
    from finengine.config import settings

    assert settings.documents_database_url == ""
    D.reset_store()
    with pytest.raises(RuntimeError, match="FINENGINE_DOCUMENTS_DATABASE_URL"):
        D.get_store()


def test_a_record_saved_before_a_field_existed_still_migrates(tmp_path, база, объекты):
    """🔴 ПОЙМАНО ЖИВЫМ ПЕРЕЕЗДОМ 25.08: пикл отдаёт объект БЕЗ нового поля.

    `meta_card` завели позже, чем записали часть документов. Питон
    восстанавливает такой объект без атрибута вовсе — обращение к нему падает
    с AttributeError, хотя в сегодняшней модели у поля есть умолчание.

    Это ровно тот дефект, ради ухода от которого переезд и делается: пикл
    разбирается только тем кодом, который его писал. Показательно, что
    споткнулся об него сам переезд — и потому проверка стоит здесь.

    ⚠️ ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ, И ПЕРВЫЙ БЫЛ НЕГОДНЫМ. Сперва я проверил мутацию
    «собирать поля через `dict(meta.__dict__)`» — она прошла зелёной, потому
    что тоже безопасна: отсутствующего ключа в словаре просто нет, и модель
    подставит умолчание. Различает только возврат ИСХОДНОГО способа —
    `model_copy` плюс обращение к атрибуту `новая.meta_card`, — на нём тест
    краснеет. Дело было не в способе сборки словаря, а в обращении к полю.
    """
    from finengine.store.migrate_documents import _перевести

    файл = tmp_path / "старый.pdf"
    файл.write_bytes(b"x")
    старая = _док(путь=str(файл))
    # Ровно то, что делает распаковка старого пикла: атрибута нет ВООБЩЕ.
    object.__delattr__(старая, "__dict__") if False else None
    del старая.__dict__["meta_card"]
    with pytest.raises(AttributeError):
        старая.meta_card  # предпосылка теста: поле действительно отсутствует

    новая = _перевести(старая, "documents/d1/старый.pdf")

    assert новая.file_path == "documents/d1/старый.pdf"
    assert новая.meta_card["legacy_file_path"] == str(файл)
    assert новая.company_name == "Северсталь", "остальные поля не должны потеряться"
