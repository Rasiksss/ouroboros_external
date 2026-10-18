"""Источники проекта: текст и карточка — объектами хранилища службы, байты оригинала — в каталоге ouroboros.

🔴 ЗАЧЕМ. `docs/rules/architecture.md` 3.1 (MUST) называет этот случай прямее
всех: «загрузки/скачивания — в объектное хранилище через стриминг, никогда на
диск… парсинг — `materialize(key)->Path` + `release`».

Проверяется контракт, а не раскладка: что источник сохраняется и читается,
что чужой проект его не видит, что временный файл разбора убирается, и что
раскладка ключей совпадает с прежней файловой — иначе переезд был бы
пересборкой, и старые загрузки пришлось бы искать вручную.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from finengine.skills import sources
from finengine.store import blobs


@pytest.fixture(autouse=True)
def на_томе(tmp_path, monkeypatch, catalog):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "s3_endpoint", "")
    blobs._cached = None
    yield tmp_path
    blobs._cached = None


async def _положить(имя: str = "план.txt", содержимое: bytes = b"a;b\n1;2\n"):
    return await sources.save_upload(user_id=7, project_id="p", filename=имя,
                                     content=содержимое)


@pytest.mark.asyncio
async def test_an_uploaded_source_comes_back(на_томе):
    meta = await _положить()

    байты, m = sources.read_source_bytes(7, "p", meta.file_id)
    assert байты == b"a;b\n1;2\n"
    assert m.filename == "план.txt"
    assert sources.read_source_text(7, "p", meta.file_id) == "a;b\n1;2\n"


@pytest.mark.asyncio
async def test_the_text_and_the_card_keep_the_old_key_layout_and_the_original_is_not_in_the_store(на_томе, catalog):
    """🔴 РАСКЛАДКА КЛЮЧЕЙ ТА ЖЕ — текст и карточка лежат там, где лежали; оригинала в хранилище службы нет.

    Мутация: изменить префикс в `sources._префикс` — тест краснеет. Оригинал живёт в каталоге ouroboros
    (ADR-0012): два источника правды о байтах — дефект, поэтому ключа `<file_id>.txt`-соседа с байтами нет.
    """
    meta = await _положить()

    store = blobs.get_blob_store()
    ключ = f"users/7/projects/p/sources/{meta.file_id}"
    assert store.exists(f"{ключ}.txt") and store.exists(f"{ключ}.meta.json")
    assert not store.exists(f"{ключ}.txt.orig") and not store.exists(f"{ключ}.csv")
    assert [b.content for b in catalog.files.values()] == [b"a;b\n1;2\n"]


@pytest.mark.asyncio
async def test_another_project_does_not_see_the_source(на_томе):
    meta = await _положить()

    assert sources.get_source_meta(7, "другой", meta.file_id) is None
    assert sources.read_source_bytes(7, "другой", meta.file_id) is None
    assert sources.list_sources(7, "другой") == []


@pytest.mark.asyncio
async def test_another_user_does_not_see_the_source(на_томе):
    meta = await _положить()

    assert sources.get_source_meta(8, "p", meta.file_id) is None
    assert sources.list_sources(8, "p") == []


@pytest.mark.asyncio
async def test_the_list_is_newest_first(на_томе):
    первый = await _положить("первый.txt")
    второй = await _положить("второй.txt")

    перечень = sources.list_sources(7, "p")
    assert [m.file_id for m in перечень] == [второй.file_id, первый.file_id]


@pytest.mark.asyncio
async def test_deleting_removes_the_file_in_the_catalog_the_text_and_the_card(на_томе, catalog):
    meta = await _положить()

    assert sources.delete_source(7, "p", meta.file_id) is True
    assert sources.get_source_meta(7, "p", meta.file_id) is None
    assert catalog.files == {}
    assert not blobs.get_blob_store().exists(f"users/7/projects/p/sources/{meta.file_id}.txt")
    assert sources.delete_source(7, "p", meta.file_id) is False


@pytest.mark.asyncio
async def test_the_temporary_copy_made_for_parsing_is_removed(на_томе, monkeypatch):
    """⚠️ Уборку временной копии для разбора обязаны ЗВАТЬ: правило «никогда на диск» без неё — «на диск, но тайком».

    Оригинал приходит из каталога байтами, парсеры берут путь: копия ложится во временный каталог и
    должна исчезнуть после разбора. Проверяется договор, а не следствие: каталог создан и убран.
    """
    created: list[str] = []
    real = tempfile.TemporaryDirectory

    class Recording(real):  # type: ignore[type-arg, misc]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self.name)

    monkeypatch.setattr(sources.tempfile, "TemporaryDirectory", Recording)

    await _положить()

    assert created, "разбор перестал класть копию во временный каталог — тест устарел"
    assert not any(Path(name).exists() for name in created)


@pytest.mark.asyncio
async def test_a_forbidden_extension_is_refused(на_томе):
    with pytest.raises(ValueError, match="not allowed"):
        await sources.save_upload(user_id=7, project_id="p", filename="вирус.exe",
                                  content=b"x")


def test_a_broken_card_does_not_hide_the_rest(на_томе):
    """Один недописанный источник не должен прятать остальные из перечня."""
    store = blobs.get_blob_store()
    store.put("users/7/projects/p/sources/битый.meta.json", "{не json".encode())
    store.put("users/7/projects/p/sources/целый.meta.json", json.dumps(
        {"file_id": "ц", "filename": "a.txt", "content_type": "text/plain",
         "size": 1, "uploaded_at": 1.0, "text_status": "ok"}).encode("utf-8"))

    assert [m.filename for m in sources.list_sources(7, "p")] == ["a.txt"]


def test_the_migration_keeps_the_relative_path_as_the_key(на_томе):
    """Переезд: ключ объекта = прежний относительный путь, без переименований."""
    from finengine.store.migrate_sources import migrate

    файл = на_томе / "users" / "7" / "projects" / "p" / "sources" / "f1.meta.json"
    файл.parent.mkdir(parents=True)
    файл.write_bytes(b"{}")

    store = blobs.get_blob_store()
    отчёт = migrate(на_томе / "users", store)

    assert отчёт == {"on_disk": 1, "uploaded": 1, "already": 0}
    assert store.exists("users/7/projects/p/sources/f1.meta.json")
    assert migrate(на_томе / "users", store)["already"] == 1
