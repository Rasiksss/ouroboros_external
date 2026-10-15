"""Контуры не делят пространство имён ни в одном общем хранилище.

🔴 ЗАЧЕМ. Требование разработчика (26.08.2026) к сосуществованию dev и prod.
Redis закрыт приставкой ключей (`test_redis_keys_carry_the_contour_prefix`).
Здесь — два оставшихся общих хранилища:

* **Postgres.** Схема стоит у ПОДКЛЮЧЕНИЯ, а не у таблиц: одни и те же
  определения таблиц обязаны работать и на Postgres, и на SQLite, а
  `MetaData(schema="dev")` на SQLite падает («unknown database dev»). SQLite на
  стенде не используется, но остался откатом, источником переездов и подложкой
  тестов. Отсюда — любой движок, собранный мимо `store/engine.py`, пишет в
  `public`, пока приложение читает `dev`. Обе стороны при этом работают, просто
  с разными данными: расхождение МОЛЧИТ. Отсюда сторож на «один вход».
* **S3.** Бакет один на хост. Без приставки объект одного контура перезаписывает
  объект другого при совпадении ключа — а ключи у нас детерминированные.

⚠️ Живых Postgres и S3 здесь нет: тесты идут без сети. Проверяется то, что
УХОДИТ в драйвер, — на подставленном двойнике.
"""
from __future__ import annotations

import pathlib
import re
import sys
import types

import pytest

from finengine.config import Settings
from finengine.store import blobs as B
from finengine.store import engine as E

_КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1] / "src" / "finengine"


# --------------------------------------------------------------------------
# Postgres: один вход, и он ставит схему контура
# --------------------------------------------------------------------------

def test_every_engine_goes_through_one_door():
    """`create_engine` мимо `store/engine.py` — движок без схемы контура."""
    мимо = []
    for путь in _КОРЕНЬ.rglob("*.py"):
        if путь.name == "engine.py" and путь.parent.name == "store":
            continue
        текст = путь.read_text(encoding="utf-8")
        for номер, строка in enumerate(текст.splitlines(), start=1):
            # ЛЮБОЙ вызов `create_engine(` — голый, sa., sqlalchemy., любой
            # алиас: слепота к квалифицированной форме — дыра, найденная
            # ревизией 27.08 (ровно так cleanup_volume ходил мимо двери).
            # Комментарии пропускаем: упоминание — не вызов.
            if строка.lstrip().startswith("#"):
                continue
            if re.search(r"create_engine\s*\(", строка):
                мимо.append(f"{путь.relative_to(_КОРЕНЬ)}:{номер}")

    assert not мимо, (
        "движок собран мимо store/engine.py — он будет писать в public, пока "
        f"приложение читает схему контура: {мимо}")


def test_the_engine_carries_the_contour_schema_on_postgres(monkeypatch):
    monkeypatch.setattr(E.config.settings, "pg_schema", "dev")
    записано = {}
    monkeypatch.setattr(E, "_create_engine",
                        lambda url, **kw: записано.update(url=url, **kw))

    E.движок("postgresql+psycopg://кто:пароль@хост/база", future=True)

    assert записано["connect_args"]["options"] == "-csearch_path=dev"


def test_sqlite_is_left_alone_because_it_has_no_schemas(monkeypatch):
    """SQLite схем не знает: `search_path` на нём — отказ, а не изоляция."""
    monkeypatch.setattr(E.config.settings, "pg_schema", "dev")
    записано = {}
    monkeypatch.setattr(E, "_create_engine",
                        lambda url, **kw: записано.update(url=url, **kw))

    E.движок("sqlite:////tmp/что-нибудь.db", future=True)

    assert "connect_args" not in записано


def test_an_empty_schema_changes_nothing(monkeypatch):
    """Пусто = как было. Иначе переезд включался бы сам, без владельца."""
    monkeypatch.setattr(E.config.settings, "pg_schema", "")
    записано = {}
    monkeypatch.setattr(E, "_create_engine",
                        lambda url, **kw: записано.update(url=url, **kw))

    E.движок("postgresql+psycopg://кто:пароль@хост/база", future=True)

    assert "connect_args" not in записано


def test_a_schema_name_is_checked_because_it_cannot_be_a_parameter():
    """Имя схемы уходит в SQL подстановкой — сужать набор символов обязаны мы."""
    with pytest.raises(ValueError):
        E.проверить_имя('dev"; drop schema public cascade; --')
    with pytest.raises(ValueError):
        E.проверить_имя("1dev")
    assert E.проверить_имя("dev_2") == "dev_2"


# --------------------------------------------------------------------------
# S3: приставка контура на каждом ключе
# --------------------------------------------------------------------------

class ЗаписнойS3:
    """Двойник boto3-клиента: помнит имена, под которыми его звали."""

    def __init__(self, полка: dict) -> None:
        self.полка = полка
        self.имена: list[str] = []

    def put_object(self, Bucket, Key, Body):  # noqa: N803 — имена от boto3
        self.имена.append(Key)
        self.полка[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        self.имена.append(Key)
        return {"Body": types.SimpleNamespace(read=lambda: self.полка[Key])}

    def head_object(self, Bucket, Key):  # noqa: N803
        self.имена.append(Key)
        if Key not in self.полка:
            raise KeyError(Key)

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.имена.append(Key)
        self.полка.pop(Key, None)

    def get_paginator(self, _имя):
        return types.SimpleNamespace(paginate=self._страницы)

    def _страницы(self, Bucket, Prefix):  # noqa: N803
        self.имена.append(Prefix)
        yield {"Contents": [{"Key": k} for k in self.полка if k.startswith(Prefix)]}


@pytest.fixture
def бакет():
    """Один бакет на оба контура — ровно та беда, от которой защищаемся."""
    return {}


@pytest.fixture
def хранилище(monkeypatch, бакет):
    """S3-хранилище на двойнике. boto3 в тестовом окружении нет — подставляем."""
    def сделать(приставка):
        записной = ЗаписнойS3(бакет)
        monkeypatch.setitem(sys.modules, "boto3",
                            types.SimpleNamespace(client=lambda *a, **k: записной))
        # botocore тоже отсутствует: `exists` ловит его ClientError.
        исключения = types.ModuleType("botocore.exceptions")
        исключения.ClientError = KeyError
        monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
        monkeypatch.setitem(sys.modules, "botocore.exceptions", исключения)
        х = B.S3BlobStore("http://нет", "finengine", "к", "с", prefix=приставка)
        return х, записной
    return сделать


def test_every_object_name_reaching_s3_carries_the_prefix(хранилище):
    х, записной = хранилище("dev/")

    х.put("documents/один.pdf", b"one")
    х.get("documents/один.pdf")
    х.exists("documents/один.pdf")
    list(х.iter_keys("documents/"))
    х.delete("documents/один.pdf")

    assert записной.имена, "двойник не звали — тест ничего не проверил"
    чужие = [и for и in записной.имена if not и.startswith("dev/")]
    assert not чужие, f"в S3 ушли имена без приставки контура: {чужие}"


def test_one_contour_does_not_overwrite_the_objects_of_another(хранилище):
    dev, _ = хранилище("dev/")
    prod, _ = хранилище("prod/")

    dev.put("documents/один.pdf", b"dev-body")
    prod.put("documents/один.pdf", b"prod-body")

    assert dev.get("documents/один.pdf") == b"dev-body"
    assert prod.get("documents/один.pdf") == b"prod-body"
    assert list(dev.iter_keys("")) == ["documents/один.pdf"]


def test_a_caller_gets_back_the_key_it_put_in(хранилище):
    """Приставка — дело хранилища. Наружу ключ возвращается как его клали."""
    dev, _ = хранилище("dev/")

    dev.put("documents/один.pdf", b"one")

    assert list(dev.iter_keys("")) == ["documents/один.pdf"]


def test_an_empty_prefix_keeps_todays_layout(хранилище):
    """Пусто = объекты в корне бакета, как сегодня на dev. Ничего не переезжает."""
    х, записной = хранилище("")

    х.put("documents/один.pdf", b"one")

    assert записной.имена == ["documents/один.pdf"]


def test_a_prefix_without_a_slash_would_swallow_the_neighbour():
    """`dev` без косой — начало `dev2`, и перечисление вычерпает соседа."""
    assert Settings(FINENGINE_S3_PREFIX="dev").s3_prefix == "dev/"
    assert Settings(FINENGINE_S3_PREFIX="/dev/").s3_prefix == "dev/"
    assert Settings(FINENGINE_S3_PREFIX="").s3_prefix == ""

# --------------------------------------------------------------------------
# Кэши хранилищ: смена схемы контура обязана давать ДРУГОЙ экземпляр
# --------------------------------------------------------------------------
#
# 🔴 ПОЙМАНО ПЕРВЫМ ПРОГОНОМ ПРОТИВ НАСТОЯЩЕГО POSTGRES (news/scenarios,
# 26.08): схема живёт внутри движка, и кэш, ключованный одним адресом, отдавал
# движок, смотрящий в ЧУЖУЮ схему. Здесь то же правило для остальных кэшей.
# SQLite-адрес годится: `движок()` схему на нём не ставит, но КЛЮЧ кэша
# обязан её учитывать — это и проверяем.


@pytest.mark.parametrize("модуль, настройка, взять", [
    ("finengine.store.journals", "journals_database_url", "get_journal_store"),
    ("finengine.store.state", "state_database_url", "get_state_store"),
    ("finengine.store.artifacts", "artifacts_database_url", "get_artifact_store"),
    ("finengine.store.runs", "runs_database_url", "get_run_store"),
    ("finengine.store.dialogs", "dialogs_database_url", "get_dialog_store"),
])
def test_a_schema_change_is_a_different_store(модуль, настройка, взять,
                                              tmp_path, monkeypatch):
    import importlib

    from finengine.config import settings

    м = importlib.import_module(модуль)
    monkeypatch.setattr(settings, настройка, f"sqlite:///{tmp_path / 'к.db'}")
    monkeypatch.setattr(м, "_cached", None)

    monkeypatch.setattr(settings, "pg_schema", "dev")
    первый = getattr(м, взять)()
    monkeypatch.setattr(settings, "pg_schema", "prod")
    второй = getattr(м, взять)()

    assert первый is not второй, (
        f"{модуль}: смена схемы контура отдала кэшированное хранилище — "
        "по одному адресу оно смотрело бы в чужую схему")
    monkeypatch.setattr(м, "_cached", None)

# --------------------------------------------------------------------------
# S3: ключ документа и честность exists
# --------------------------------------------------------------------------


def test_a_long_filename_is_capped_inside_the_key():
    """MinIO кладёт ключ на ФС: сегмент длиннее 255 байт — отказ на put.

    Кап живёт ВНУТРИ document_key: у вызывающего он защищал бы только
    загрузку, а переезд шёл бы своим, неусечённым путём. Мутация: убрать
    усечение — тест краснеет.
    """
    from finengine.store.blobs import document_key

    длинное = "Промежуточная бухгалтерская отчётность " * 10 + ".pdf"
    ключ = document_key("did", длинное)
    сегмент = ключ.rsplit("/", 1)[-1]
    assert len(сегмент.encode("utf-8")) <= 210, "имя в ключе не усечено"
    assert сегмент.endswith(".pdf"), "усечение съело расширение"
    короткое = document_key("did", "отчёт.pdf")
    assert короткое == "documents/did/отчёт.pdf", "короткое имя не должно меняться"


def test_exists_tells_no_access_apart_from_no_object(хранилище):
    """403 — это «сломан доступ», а не «объекта нет».

    Прежний код глотал любой ClientError: протухший ключ доступа читался как
    отсутствие объекта, чтение уходило в откат на диск и отдавало старьё.
    """
    import sys

    х, записной = хранилище("dev/")

    class _Отказ(Exception):
        def __init__(self, код, статус):
            self.response = {"Error": {"Code": код},
                             "ResponseMetadata": {"HTTPStatusCode": статус}}

    sys.modules["botocore.exceptions"].ClientError = _Отказ

    def _head(Bucket, Key):  # noqa: N803
        if Key.endswith("нет.pdf"):
            raise _Отказ("404", 404)
        raise _Отказ("AccessDenied", 403)

    записной.head_object = _head
    assert х.exists("documents/нет.pdf") is False
    with pytest.raises(_Отказ):
        х.exists("documents/есть.pdf")

