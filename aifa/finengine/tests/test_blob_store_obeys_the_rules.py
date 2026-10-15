"""Хранилище объектов: одно поведение на обе реализации, и обход пути закрыт.

🔴 ЗАЧЕМ (решение владельца 25.08.2026: «Давай делать так чтобы это
соответствовало правилам разработки, и ставь S3»). `docs/rules/architecture.md`
называет и правило, и интерфейс:

  * 3.1 (🔴 MUST) «всё durable — во внешние хранилища: Postgres, Redis,
    S3/MinIO… загрузки/скачивания — в объектное хранилище через стриминг,
    никогда на диск (парсинг — `materialize(key)->Path` + `release`)»;
  * «Зависеть от абстракции, не реализации: blob (→ `BlobStore`/Local/S3)».

ОБЕ РЕАЛИЗАЦИИ БОЕВЫЕ, а не «одна настоящая и одна заглушка»: у нас
docker-compose на одном VPS, у принимающей команды — Kubernetes с read-only
корневой ФС, где локальная ветка не запустится вовсе. Поэтому контракт
проверяется одними тестами для обеих; расхождение поведения — дефект, а не
особенность бэкенда.

⚠️ S3-ветка здесь НЕ ПРОВЕРЯЕТСЯ ЖИВЬЁМ. Для неё нужен поднятый MinIO, а тесты
обязаны идти без сети. Проверяется то, что можно проверить без него: контракт
локальной ветки, общая защита ключа и переключатель. Живая проверка S3 —
смоук на стенде, и это сказано вслух, а не спрятано за зелёным тестом.
"""
from __future__ import annotations

import io

import pytest

from finengine.store.blobs import LocalBlobStore, _safe_key, get_blob_store


@pytest.fixture
def store(tmp_path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def test_a_blob_survives_a_round_trip(store):
    store.put("documents/a.pdf", b"%PDF-1.7 ...")
    assert store.get("documents/a.pdf") == b"%PDF-1.7 ..."
    assert store.exists("documents/a.pdf")


def test_a_stream_is_accepted_not_only_bytes(store):
    """Правило требует СТРИМИНГА: отчёт на 10 МБ незачем держать в памяти."""
    store.put("documents/b.pdf", io.BytesIO(b"x" * 4096))
    assert store.get("documents/b.pdf") == b"x" * 4096


def test_a_reader_never_sees_a_half_written_object(store):
    """Читатель в момент записи видит либо старое, либо новое — не половину.

    🔴 ПЕРВАЯ РЕДАКЦИЯ ЭТОГО ТЕСТА БЫЛА НЕГОДНОЙ. Она проверяла, что рядом не
    осталось `.tmp`-файлов, — а это верно и тогда, когда временного файла не
    создают ВООБЩЕ. Мутация «писать напрямую, без временного файла» проходила
    зелёной, то есть тест не сторожил ничего.

    Здесь свойство проверяется по существу: пока один поток перезаписывает
    объект на 8 МБ, другой читает его в цикле. Прямая запись даёт короткое
    окно, в котором файл уже усечён, но ещё не дописан, — и читатель поймает
    обрезок. Запись через временный файл с `replace` такого окна не имеет.

    Цена ошибки не абстрактная: обрезок здесь — это PDF пользователя,
    прочитанный наполовину.
    """
    import threading

    старое_знач = b"A" * (8 * 1024 * 1024)
    новое_знач = b"B" * (8 * 1024 * 1024)
    store.put("documents/c.pdf", старое_знач)

    увиденное: list[bytes] = []
    стоп = threading.Event()

    def читатель() -> None:
        while not стоп.is_set():
            try:
                увиденное.append(store.get("documents/c.pdf"))
            except FileNotFoundError:
                увиденное.append(b"")  # исчезнувший объект — тоже не «старое или новое»

    t = threading.Thread(target=читатель, daemon=True)
    t.start()
    try:
        for _ in range(6):
            store.put("documents/c.pdf", новое_знач)
            store.put("documents/c.pdf", старое_знач)
    finally:
        стоп.set()
        t.join(timeout=5)

    assert увиденное, "читатель не успел ни разу — тест ничего не проверил"
    рваные = [len(v) for v in увиденное if v not in (старое_знач, новое_знач)]
    assert not рваные, (
        f"читатель поймал объект в промежуточном состоянии, длины: {рваные[:5]}. "
        "Запись обязана идти через временный файл и replace")


def test_keys_cannot_escape_the_store(store):
    """🔴 Ключи приходят из имён файлов и doc_id — обход пути тут реален.

    Без этой проверки `../../etc/passwd` в ключе означал бы чтение и запись
    где угодно на томе. Правило одно на обе ветки: у S3 пространство имён
    плоское, но безопасность не должна зависеть от того, что включено сегодня.
    """
    for злой in ("../secret", "documents/../../etc/passwd", "..", "", "   "):
        with pytest.raises(ValueError):
            _safe_key(злой)

    # ⚠️ А ВОТ ЭТО НЕ ПОБЕГ, и первая редакция теста ошибочно требовала отказа.
    # Ведущий слэш снимается, и `/etc/passwd` становится обычным ключом ВНУТРИ
    # хранилища — файл ляжет в <корень>/etc/passwd и ничего чужого не тронет.
    # Требовать здесь ValueError значило бы запретить законные ключи, начатые
    # со слэша (их шлёт фронт), — соседний тест как раз на это.
    assert _safe_key("/etc/passwd") == "etc/passwd"


def test_a_normal_nested_key_is_allowed(store):
    """Отрицательный контроль к предыдущему: обычные ключи не должны падать."""
    assert _safe_key("documents/2026/отчёт.pdf") == "documents/2026/отчёт.pdf"
    assert _safe_key("/documents/a.pdf") == "documents/a.pdf"


def test_materialize_gives_a_path_a_parser_can_open(store):
    """Парсеры PDF работают с ПУТЁМ, а не с потоком — ради этого метод и есть."""
    store.put("documents/d.pdf", "содержимое".encode())
    p = store.materialize("documents/d.pdf")
    assert p.is_file() and p.read_bytes() == "содержимое".encode()


def test_release_of_a_local_blob_does_not_delete_the_original(store):
    """🔴 СИММЕТРИЯ ИНТЕРФЕЙСА ЗДЕСЬ ОБМАНЧИВА, И ЭТО ПРОВЕРЯЕТСЯ.

    В S3-ветке `materialize` скачивает КОПИЮ, и `release` её убирает. В
    локальной он отдаёт ОРИГИНАЛ — удалить его значило бы стереть документ
    пользователя вызовом, который выглядит как уборка мусора.
    """
    store.put("documents/e.pdf", "важное".encode())
    p = store.materialize("documents/e.pdf")
    store.release(p)
    assert store.exists("documents/e.pdf"), "release стёр оригинал документа"


def test_missing_object_is_an_honest_error(store):
    with pytest.raises(FileNotFoundError):
        store.materialize("documents/нет-такого.pdf")


def test_delete_and_listing_agree(store):
    store.put("documents/f.pdf", b"1")
    store.put("exports/g.xlsx", b"2")
    assert set(store.iter_keys()) == {"documents/f.pdf", "exports/g.xlsx"}
    assert set(store.iter_keys("documents/")) == {"documents/f.pdf"}
    store.delete("documents/f.pdf")
    assert not store.exists("documents/f.pdf")


def test_the_default_stays_local_until_someone_sets_the_endpoint():
    """Код ничего не включает сам: пустой endpoint = файлы, как было."""
    from finengine.config import settings
    assert settings.s3_endpoint == "", (
        "endpoint S3 выставлен по умолчанию — переезд хранилища обязан быть "
        "решением человека, а не побочным эффектом обновления")
    assert type(get_blob_store()).__name__ == "LocalBlobStore"


def test_the_rules_name_this_very_interface():
    """Сторож на ИМЕНА: правило называет `BlobStore`/Local/S3 и materialize.

    Не педантизм: чужой человек придёт по правилу и будет искать именно эти
    имена. Переименуют — правило перестанет вести к коду.
    """
    import finengine.store.blobs as b
    for имя in ("BlobStore", "LocalBlobStore", "S3BlobStore", "get_blob_store"):
        assert hasattr(b, имя), f"из store/blobs.py пропало имя из правила: {имя}"
    for метод in ("put", "get", "exists", "delete", "iter_keys", "materialize", "release"):
        assert hasattr(b.LocalBlobStore, метод), f"в BlobStore нет метода {метод}"
