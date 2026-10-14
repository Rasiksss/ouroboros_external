"""Источники проекта: байты, текст и карточка — объектами, а не файлами.

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

import pytest

from finengine.skills import sources
from finengine.store import blobs


@pytest.fixture(autouse=True)
def на_томе(tmp_path, monkeypatch):
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
async def test_the_key_layout_matches_the_old_file_layout(на_томе):
    """🔴 РАСКЛАДКА ТА ЖЕ — переезд это копирование, а не пересборка.

    Мутация: изменить префикс в `sources._префикс` — тест краснеет. Без этого
    свойства старые загрузки пришлось бы переименовывать поштучно, и заметно
    это стало бы у пользователя пустой панелью «Источники».
    """
    meta = await _положить()

    ключи = set(blobs.get_blob_store().iter_keys("users/7/projects/p/sources/"))
    assert f"users/7/projects/p/sources/{meta.file_id}.txt" in ключи
    assert f"users/7/projects/p/sources/{meta.file_id}.meta.json" in ключи


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
async def test_deleting_removes_all_three_pieces(на_томе):
    meta = await _положить()

    assert sources.delete_source(7, "p", meta.file_id) is True
    assert sources.get_source_meta(7, "p", meta.file_id) is None
    assert list(blobs.get_blob_store().iter_keys("users/7/projects/p/sources/")) == []
    assert sources.delete_source(7, "p", meta.file_id) is False


@pytest.mark.asyncio
async def test_every_materialize_is_paired_with_a_release(на_томе, monkeypatch):
    """⚠️ Уборку временного файла разбора обязаны ЗВАТЬ.

    🔴 ПЕРВАЯ РЕДАКЦИЯ ЭТОГО ТЕСТА НИЧЕГО НЕ ПРОВЕРЯЛА, и это стоит записать.
    Она сравнивала содержимое системного каталога временных файлов до и после
    загрузки — и мутация «убрать `release`» её не покраснила. Причина в том,
    что у ЛОКАЛЬНОЙ ветки `materialize` отдаёт путь ОРИГИНАЛА, а `release` —
    пустой: временного файла там не возникает вовсе. Временный файл появляется
    только у S3-ветки, а она в тестах не поднимается (нужна сеть).

    Значит проверять надо не следствие, а сам договор: сколько раз попросили
    файл — столько раз и вернули. Это видно на любой реализации и краснеет от
    снятия `release`. Без парности правило «никогда на диск» превращается в
    «на диск, но тайком», и на S3 временные файлы копятся до заполнения тома.
    """
    настоящее = blobs.get_blob_store()
    счёт = {"materialize": 0, "release": 0}

    class Считающее:
        def __getattr__(self, имя):
            return getattr(настоящее, имя)

        def materialize(self, key):
            счёт["materialize"] += 1
            return настоящее.materialize(key)

        def release(self, path):
            счёт["release"] += 1
            return настоящее.release(path)

    monkeypatch.setattr(blobs, "get_blob_store", lambda: Считающее())
    await _положить()

    assert счёт["materialize"] >= 1, "разбор перестал брать файл — тест устарел"
    assert счёт["release"] == счёт["materialize"]


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
