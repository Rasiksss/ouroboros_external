"""Объектное хранилище: PDF, выгрузки, артефакты. Файлы или S3 — по настройке.

ЗАЧЕМ. `docs/rules/architecture.md`:

  * 3.1 (🔴 MUST) «Нет состояния на файловой системе контейнера… всё durable —
    во внешние хранилища: Postgres, Redis, S3/MinIO… загрузки/скачивания — в
    объектное хранилище через стриминг, никогда на диск (large files —
    StreamingResponse, парсинг — `materialize(key)->Path` + `release`)»;
  * 1.x «Зависеть от абстракции, не реализации: blob (→ `BlobStore`/Local/S3)».

Интерфейс здесь назван так, как названо в правиле, и не иначе: чужой человек,
пришедший по правилу, должен найти именно это.

🔴 ПОЧЕМУ `materialize`, А НЕ ПРОСТО `get`. Парсеры PDF (IDP-OCR, pdfplumber)
работают с ПУТЁМ на диске, а не с потоком байт. Значит на время разбора объект
всё равно должен лечь во временный файл — и вопрос лишь в том, кто его уберёт.
Пара `materialize`/`release` делает уборку обязанностью вызывающего и видимой
в коде; без неё временные файлы копятся молча, и правило «никогда на диск»
превращается в «на диск, но тайком».

⚠️ ЛОКАЛЬНАЯ РЕАЛИЗАЦИЯ — НЕ ЗАГЛУШКА И НЕ ВРЕМЕННАЯ. У нас один VPS и
docker-compose, S3 включается профилем; у команды, которая примет проект, —
Kubernetes с read-only корневой ФС, где локальная ветка не запустится вовсе.
Обе ветки боевые для своего контура, поэтому обе покрыты одними тестами.

ПЕРЕКЛЮЧАТЕЛЬ. Пустой `FINENGINE_S3_ENDPOINT` (по умолчанию) — файлы на томе,
ровно как было. Непустой — S3/MinIO. Момент переезда выбирает владелец; код
ничего не включает сам, как у диалогов, учёток и прогонов.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

from finengine import config
from finengine.config import settings

log = logging.getLogger(__name__)


class BlobStore(Protocol):
    """Объект по ключу. Ключ — строка вида `documents/<doc_id>.pdf`."""

    def put(self, key: str, data: bytes | BinaryIO) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def iter_keys(self, prefix: str = "") -> Iterator[str]: ...
    def materialize(self, key: str) -> Path: ...
    def release(self, path: Path) -> None: ...


def _safe_key(key: str) -> str:
    """Ключ не должен уводить за пределы хранилища.

    🔴 БЕЗ ЭТОГО ЛОКАЛЬНАЯ ВЕТКА — ДЫРА. Ключи приходят из имён файлов и
    `doc_id`; `../../etc/passwd` в ключе означал бы чтение и запись где угодно
    на томе. У S3 такой проблемы нет (там плоское пространство имён), но
    проверка стоит здесь одна на обе ветки: правило безопасности не должно
    зависеть от того, какой бэкенд включён сегодня.
    """
    очищен = key.strip().lstrip("/")
    if not очищен or ".." in Path(очищен).parts:
        raise ValueError(f"недопустимый ключ объекта: {key!r}")
    return очищен


class LocalBlobStore:
    """Файлы на томе. Текущее поведение, оформленное интерфейсом."""

    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._root / _safe_key(key)

    def put(self, key: str, data: bytes | BinaryIO) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Через временный файл и `replace`: читатель никогда не увидит
        # наполовину записанный объект.
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "wb") as f:
            if isinstance(data, bytes):
                f.write(data)
            else:
                shutil.copyfileobj(data, f)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        корень = self._root
        for p in sorted(корень.rglob("*")):
            if not p.is_file() or p.suffix == ".tmp":
                continue
            ключ = str(p.relative_to(корень))
            if ключ.startswith(prefix):
                yield ключ

    def materialize(self, key: str) -> Path:
        """Объект уже лежит файлом — отдаём его путь, копию не делаем."""
        p = self._path(key)
        if not p.is_file():
            raise FileNotFoundError(key)
        return p

    def release(self, path: Path) -> None:
        """Ничего не удаляем: это ОРИГИНАЛ, а не временная копия.

        Симметрия с S3-веткой здесь обманчива и потому названа вслух: там
        `release` убирает временный файл, здесь удаление стёрло бы сам объект.
        """
        return None


class S3BlobStore:
    """S3/MinIO. Клиент создаётся лениво — образ без S3 остаётся рабочим."""

    def __init__(self, endpoint: str, bucket: str, access_key: str,
                 secret_key: str, region: str = "us-east-1",
                 prefix: str | None = None):
        self._bucket = bucket
        # Бакет один на хост, как Redis и Postgres: контуры разводит приставка.
        # Пусто = как было, объекты в корне бакета.
        # Через модуль, а не через ссылку с импорта: цена ошибки здесь —
        # «молча не тот контур» (разбор — в `store/engine.схема`).
        self._prefix = config.settings.s3_prefix if prefix is None else prefix
        self._временные: set[Path] = set()
        import boto3  # noqa: PLC0415 — тяжёлая зависимость, только для этой ветки

        self._s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name=region,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        )

    def _k(self, key: str) -> str:
        """Имя объекта в бакете. Приставка контура ставится ЗДЕСЬ и только здесь."""
        return f"{self._prefix}{_safe_key(key)}"

    def ensure_bucket(self) -> None:
        """Создать корзину, если её нет. Идемпотентно."""
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._s3.create_bucket(Bucket=self._bucket)

    def put(self, key: str, data: bytes | BinaryIO) -> None:
        k = self._k(key)
        if isinstance(data, bytes):
            self._s3.put_object(Bucket=self._bucket, Key=k, Body=data)
        else:
            self._s3.upload_fileobj(data, self._bucket, k)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=self._k(key))["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._k(key))
            return True
        except ClientError as e:
            # 🔴 False — ТОЛЬКО «такого ключа нет». Прежний код глотал ЛЮБОЙ
            # ClientError, и 403 (протухший ключ доступа, чужой бакет) читался
            # как «объекта нет»: чтение уходило в откат на диск и отдавало
            # старьё, а уборка считала копию неподтверждённой. Сломанный доступ
            # обязан звучать как сломанный доступ.
            код = str(e.response.get("Error", {}).get("Code", ""))
            статус = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if код in ("404", "NoSuchKey") or статус == 404:
                return False
            raise

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=self._k(key))

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        # Отдаём имена БЕЗ приставки: вызывающий про контур не знает, и его
        # ключ должен вернуться таким же, каким он его клал.
        срез = len(self._prefix)
        страницы = self._s3.get_paginator("list_objects_v2")
        for стр in страницы.paginate(Bucket=self._bucket,
                                     Prefix=f"{self._prefix}{prefix}"):
            for o in стр.get("Contents", ()):
                yield o["Key"][срез:]

    def materialize(self, key: str) -> Path:
        """Скачать во временный файл — парсеру нужен путь, а не поток."""
        k = self._k(key)
        суффикс = Path(k).suffix
        fd, имя = tempfile.mkstemp(suffix=суффикс, prefix="blob-")
        p = Path(имя)
        import os  # noqa: PLC0415

        os.close(fd)
        self._s3.download_file(self._bucket, k, str(p))
        self._временные.add(p)
        return p

    def release(self, path: Path) -> None:
        """Убрать временный файл. Чужой путь не трогаем — только свой.

        Проверка по множеству, а не по каталогу: `release` чужого пути молча
        удалял бы файлы, о которых хранилище ничего не знает.
        """
        p = Path(path)
        if p in self._временные:
            p.unlink(missing_ok=True)
            self._временные.discard(p)


_cached: tuple[tuple, BlobStore] | None = None


def get_blob_store() -> BlobStore:
    """ПЕРЕКЛЮЧАТЕЛЬ: S3 — если настроен endpoint, иначе файлы, как было."""
    global _cached
    endpoint = (settings.s3_endpoint or "").strip()
    ключ = (endpoint, settings.s3_bucket, settings.s3_prefix, str(settings.data_dir))
    if _cached is not None and _cached[0] == ключ:
        return _cached[1]
    if endpoint:
        store: BlobStore = S3BlobStore(
            endpoint, settings.s3_bucket, settings.s3_access_key,
            settings.s3_secret_key, settings.s3_region,
        )
    else:
        store = LocalBlobStore(Path(settings.data_dir) / "blobs")
    # Ни endpoint, ни ключи не печатаем — в них секрет.
    log.info("хранилище объектов: %s", "S3" if endpoint else "файлы на томе")
    _cached = (ключ, store)
    return store

@contextmanager
def document_file(file_path: str) -> Iterator[Path]:
    """Путь к файлу документа — из объектного хранилища ИЛИ с тома.

    🔴 ЗАЧЕМ ОДНА ФУНКЦИЯ НА ОБА МИРА. После переезда `DocumentMeta.file_path`
    держит КЛЮЧ объекта (`documents/<id>/<имя>.pdf`), а до переезда — путь на
    диске (`/app/data/documents/<id>_<имя>.pdf`). Оба вида живут одновременно:
    переключатель можно выключить, часть записей может остаться старой, а прод
    вообще ещё не переезжал.

    Разбирать это в каждом потребителе значило бы завести четыре копии одного
    решения — ровно тот класс дефекта, который стоил нам больше всего за месяц.
    Здесь оно принимается ОДИН раз, по факту, а не по догадке: спрашиваем
    хранилище, есть ли такой объект, и только потом смотрим на диск.

    Использовать так — и `release` тогда не забудется:

        with document_file(meta.file_path) as путь:
            parse_pdf(путь, meta)

    ⚠️ ВРЕМЕННАЯ КОПИЯ УБИРАЕТСЯ ВСЕГДА, включая падение разбора: иначе
    `/tmp` наполнялся бы отчётами по 5 МБ, и правило «никогда на диск»
    держалось бы ровно до первой ошибки.
    """
    if not (file_path or "").strip():
        raise FileNotFoundError("у документа не записан путь к файлу")

    store = get_blob_store()
    # Ключ или путь — решаем ПО ФАКТУ наличия объекта, а не по виду строки.
    ключ = file_path.lstrip("/")
    if ".." not in Path(ключ).parts and store.exists(ключ):
        путь = store.materialize(ключ)
        try:
            yield путь
        finally:
            store.release(путь)
        return

    прямой = Path(file_path)
    if not прямой.is_file():
        raise FileNotFoundError(file_path)
    yield прямой


def document_key(doc_id: str, filename: str) -> str:
    """Ключ объекта для документа. Одно место, где он формируется.

    Совпадает с тем, что кладёт переезд (`store/migrate_documents.py`): разойдись
    они — новые загрузки легли бы мимо перенесённых, и найти это можно было бы
    только глазами.

    🔴 ИМЯ КАПИТСЯ ПО БАЙТАМ — здесь, а не у вызывающего. MinIO кладёт ключ на
    файловую систему, и её лимит 255 байт на сегмент бьёт по длинным кириллическим
    именам банковских отчётов так же, как бил по диску (реальный инцидент, из-за
    которого кап на диске и появился — api/main.py, загрузка). Кап у вызывающего
    защищал бы только загрузку; переезд шёл бы своим, неусечённым путём.
    """
    имя = Path(filename).name
    стем, точка, расш = имя.rpartition(".")
    стем = стем or имя
    суффикс = f".{расш}" if точка else ""
    бюджет = 200 - len(суффикс.encode("utf-8"))
    усечённый = стем.encode("utf-8")[:max(бюджет, 8)].decode("utf-8", errors="ignore")
    return f"documents/{doc_id}/{усечённый}{суффикс}"

# ── Распознанный текст документа ────────────────────────────────────────────
#
# 🔴 ОДНО МЕСТО ВМЕСТО СЕМИ, И ОБЯЗАТЕЛЬНО С ОТКАТОМ НА ФАЙЛ.
#
# Сайдкар `<doc_id>.txt` собирали и читали в семи местах, каждое своим
# `settings.docs_dir / f"{doc_id}.txt"`. Правило architecture.md 3.1 требует
# объектного хранилища, но важнее другое: пока путь собирается в семи местах,
# перенос обязан быть сделан в семи местах, и промах в одном виден только у
# пользователя.
#
# ⚠️ ОТКАТ НА ФАЙЛ — НЕ ПЕРЕСТРАХОВКА, А ЛЕЧЕНИЕ РЕАЛЬНОГО ОТКАЗА (26.08.2026).
# Первая редакция переноса перевела ПИСАТЕЛЯ и шесть читателей, а седьмого —
# `skills/two_stage_extractor` — оставила на файле сознательно: он исполняется
# в запечатанной комнате, где текст лежит локально. Но тот же модуль работает и
# на бэкенде. Результат: текст публиковался в хранилище, извлечение ждало файла
# и добирало `_wait_for_pdf_text` пять раз по шестьдесят секунд. Ход вис на ПЯТЬ
# МИНУТ, прогон вырос с 2:13 до 7:15, а виновным выглядел кто угодно, кроме
# настоящей причины.
#
# Поэтому: спрашиваем хранилище, и ЕСЛИ ТАМ ПУСТО — смотрим файл. Один и тот же
# код тогда верен и на бэкенде (текст в хранилище), и в комнате (текст файлом),
# и в середине переезда (часть там, часть тут).

_ТЕКСТ = "documents/{doc_id}/text.txt"


def document_text_key(doc_id: str) -> str:
    """Ключ объекта с распознанным текстом. Собирается ровно здесь."""
    return _ТЕКСТ.format(doc_id=str(doc_id).strip())


def _файл_текста(doc_id: str) -> Path:
    """Прежнее место сайдкара. Нужен для отката — см. блок выше."""
    return Path(settings.docs_dir) / f"{doc_id}.txt"


def save_document_text(doc_id: str, text: str) -> None:
    """Опубликовать распознанный текст.

    ⚠️ ПУСТОЙ ТЕКСТ НЕ ПУБЛИКУЕМ. Иначе статус документа флипается в «готово», а
    анализ на пустом тексте отвечает «ещё обрабатывается» — пользователь видит
    два противоречащих ответа об одном документе.
    """
    if not (text or "").strip():
        return
    get_blob_store().put(document_text_key(doc_id), text.encode("utf-8"))


def document_text(doc_id: str, limit: int | None = None) -> str | None:
    """Текст документа: из хранилища, иначе с диска. `None` — его ещё нет.

    🔴 БЕЗ `limit` ОТДАЁТСЯ ВЕСЬ ТЕКСТ, и умолчание здесь не мелочь. Роут
    `/internal/doc-text` кормит станцию ПОЛНЫМ документом — извлечение у нас
    полноконтекстное. Умолчание в 200 000 символов молча резало бы отчёт МСФО
    (~280 000) на четверти, и увидели бы это не отказом, а недостающими
    показателями в конце таблицы. Порог просит тот, кому он нужен: чат режет
    контекст, потому что у шлюза лимит запроса."""
    ключ = document_text_key(doc_id)
    store = get_blob_store()
    if store.exists(ключ):
        текст = store.get(ключ).decode("utf-8", errors="ignore")
        if текст.strip():
            return текст[:limit]
    файл = _файл_текста(doc_id)
    if файл.is_file():
        текст = файл.read_text(encoding="utf-8", errors="ignore")
        if текст.strip():
            return текст[:limit]
    return None


def delete_document_text(doc_id: str) -> None:
    """Убрать текст. Зовётся для временных публикаций станции.

    Прибираем ОБА места: объект и файл. Временный текст живёт секунды,
    но посреди переезда он мог лечь на диск, а убираться — из хранилища,
    и тогда мусор копился бы молча."""
    get_blob_store().delete(document_text_key(doc_id))
    _файл_текста(doc_id).unlink(missing_ok=True)


def document_text_exists(doc_id: str) -> bool:
    """Есть ли готовый текст. Пустой не считается готовым — см. save."""
    return document_text(doc_id, limit=1) is not None


async def document_text_when_ready(
    doc_id: str, *, timeout_s: float = 240.0, poll_s: float = 1.0,
    on_wait=None, limit: int | None = None,
) -> str | None:
    """Текст, дождавшись распознавания. `None` — не дождались.

    `on_wait(прошло_секунд)` зовётся на каждом круге: вызывающий может подать
    признак жизни в поток, чтобы человек видел работу, а не тишину.

    🔴 ОЖИДАНИЕ ЖИВЁТ ЗДЕСЬ ОДНО НА ВЕСЬ КОД. Оно было написано ДВАЖДЫ —
    в `llm/tools._fe_doc_text_wait` и в `api/internal_docs._text_when_ready`, —
    и докстринг второго сам жаловался: «правило жило в одном месте из двух».
    Причина у обоих одна: человек прикладывает файл и ТЕМ ЖЕ ходом задаёт
    вопрос, а распознавание идёт фоном и текст ложится на секунды позже.
    """
    import asyncio  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    начало = _time.monotonic()
    while True:
        текст = document_text(doc_id, limit)
        if текст is not None:
            return текст
        прошло = _time.monotonic() - начало
        if прошло >= timeout_s:
            return None
        if on_wait is not None:
            await on_wait(прошло)
        await asyncio.sleep(poll_s)


# ─────────────────── СЫРОЙ ОТВЕТ ОБЛАЧНОГО OCR ───────────────────
# Второй сайдкар документа рядом с текстом: полный ответ IDP-OCR со страницами
# и таблицами. Он ДОРОГОЙ — распознавание идёт около четырёх секунд на
# страницу, и на банковском отчёте это десять минут. Потерять его значит
# заплатить за него второй раз.
#
# Путь `docs_dir / f"{doc_id}.idpocr.v2.json"` строили ЧЕТЫРЕ модуля плюс
# админский роут с перебором пары v2/v1 — тот же класс «одно решение в
# нескольких местах», что и у текста.

_IDP = "documents/{doc_id}/idpocr.v{v}.json"


def document_idp_key(doc_id: str, v: int = 2) -> str:
    """Ключ сырого ответа OCR. Версия — часть ключа, а не догадка по имени."""
    return _IDP.format(doc_id=str(doc_id).strip(), v=int(v))


def _файл_idp(doc_id: str, v: int) -> Path:
    """Прежнее место сайдкара. Нужен для отката — см. `document_text`."""
    суффикс = ".idpocr.v2.json" if v == 2 else ".idpocr.json"
    return Path(settings.docs_dir) / f"{doc_id}{суффикс}"


def document_idp_raw(doc_id: str) -> bytes | None:
    """Сырой ответ OCR: из хранилища, иначе с диска. Сначала v2, потом v1.

    Порядок версий сохранён от прежнего кода: v2 — нынешний постраничный
    конвейер, v1 — прогоны до смены клиента OCR. Отдать v1 там, где есть v2,
    значило бы молча вернуть худшее распознавание.
    """
    store = get_blob_store()
    for v in (2, 1):
        ключ = document_idp_key(doc_id, v)
        if store.exists(ключ):
            return store.get(ключ)
        файл = _файл_idp(doc_id, v)
        if файл.is_file():
            return файл.read_bytes()
    return None


def document_idp_exists(doc_id: str) -> bool:
    """Есть ли сохранённый ответ OCR — любой версии."""
    return document_idp_raw(doc_id) is not None


@contextmanager
def document_idp_sink(doc_id: str) -> Iterator[Path]:
    """Путь, куда клиент OCR пишет сырой ответ; на выходе — публикация.

    🔴 ПОЧЕМУ ЧЕРЕЗ ВРЕМЕННЫЙ ФАЙЛ, А НЕ ПРЯМО В ХРАНИЛИЩЕ. Клиент OCR
    принимает `save_raw_to: Path` и пишет туда сам — переделывать его под
    поток означало бы менять код, который к переезду отношения не имеет.
    Здесь он получает временный путь, а объект публикуется по выходе.

    Пустой или ненаписанный файл НЕ публикуется: отказ распознавания не должен
    оставлять в хранилище пустышку, из-за которой повторный прогон решит, что
    ответ уже есть.
    """
    fd, имя = tempfile.mkstemp(suffix=".idpocr.v2.json", prefix="idp-")
    import os  # noqa: PLC0415

    os.close(fd)
    временный = Path(имя)
    try:
        yield временный
        if временный.is_file() and временный.stat().st_size > 0:
            with open(временный, "rb") as f:
                get_blob_store().put(document_idp_key(doc_id, 2), f)
    finally:
        временный.unlink(missing_ok=True)


@contextmanager
def document_idp_file(doc_id: str) -> Iterator[Path | None]:
    """Сырой ответ OCR ФАЙЛОМ на время работы. `None` — его нет.

    Симметрично `document_file`: кому нужен путь, тот получает временный файл и
    возвращает его по выходе. Большинству нужны байты — им `document_idp_raw`.
    """
    сырьё = document_idp_raw(doc_id)
    if сырьё is None:
        yield None
        return
    fd, имя = tempfile.mkstemp(suffix=".idpocr.json", prefix="idp-")
    import os  # noqa: PLC0415

    os.close(fd)
    временный = Path(имя)
    временный.write_bytes(сырьё)
    try:
        yield временный
    finally:
        временный.unlink(missing_ok=True)
