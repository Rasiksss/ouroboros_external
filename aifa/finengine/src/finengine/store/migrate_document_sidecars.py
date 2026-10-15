"""Перенос сайдкаров документа в объектное хранилище: текст и ответ OCR.

    docker compose exec backend python -m finengine.store.migrate_document_sidecars --dry-run
    docker compose exec backend python -m finengine.store.migrate_document_sidecars

🔴 ЭТОТ ПЕРЕЕЗД — УСЛОВИЕ ЛЮБОЙ УБОРКИ ТОМА, И ЭТО ГЛАВНОЕ, ЧТО НАДО ЗНАТЬ.
Сами PDF уже лежат в S3, а рядом с ними на томе — два сайдкара, которых там
НЕТ: распознанный текст (`<doc_id>.txt`) и сырой ответ облачного OCR
(`<doc_id>.idpocr.v2.json`). Читаются они через `store/blobs` с откатом на
файл, поэтому всё работает и выглядит переехавшим. Стереть каталог документов
до этого переезда — значит потерять распознавание всех документов; ответ OCR
стоит около четырёх секунд на страницу, то есть на банковском отчёте десять
минут работы облака.

Проверено на стенде 26.08.2026: 158 PDF в хранилище против 170 файлов текста и
146 ответов OCR на томе.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ: что уже есть в хранилище, не заливаем заново.
Пустые сайдкары пропускаем — пустой текст не публикуется вовсе (иначе документ
показал бы «распознан», а извлечение на нём — «ещё обрабатывается»).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from finengine.config import settings
from finengine.store.blobs import (
    BlobStore, document_idp_key, document_text_key, get_blob_store,
)


def migrate(docs_dir: Path, blobs: BlobStore, dry_run: bool = False) -> dict:
    отчёт = {"texts": 0, "texts_uploaded": 0, "texts_already": 0, "texts_empty": 0,
             "idp": 0, "idp_uploaded": 0, "idp_already": 0}
    if not docs_dir.is_dir():
        return отчёт

    for файл in sorted(docs_dir.glob("*.txt")):
        отчёт["texts"] += 1
        if файл.stat().st_size == 0:
            отчёт["texts_empty"] += 1
            continue
        ключ = document_text_key(файл.stem)
        if blobs.exists(ключ):
            отчёт["texts_already"] += 1
            continue
        отчёт["texts_uploaded"] += 1
        if not dry_run:
            with open(файл, "rb") as f:
                blobs.put(ключ, f)

    for суффикс, версия in ((".idpocr.v2.json", 2), (".idpocr.json", 1)):
        for файл in sorted(docs_dir.glob(f"*{суффикс}")):
            # `Path.stem` снимает только последнее расширение: у
            # `x.idpocr.v2.json` он даст `x.idpocr.v2`. Режем по суффиксу.
            doc_id = файл.name[: -len(суффикс)]
            отчёт["idp"] += 1
            ключ = document_idp_key(doc_id, версия)
            if blobs.exists(ключ):
                отчёт["idp_already"] += 1
                continue
            отчёт["idp_uploaded"] += 1
            if not dry_run:
                with open(файл, "rb") as f:
                    blobs.put(ключ, f)
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Перенос сайдкаров документа (текст, ответ OCR) в хранилище")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    blobs = get_blob_store()
    if hasattr(blobs, "ensure_bucket"):
        blobs.ensure_bucket()

    о = migrate(Path(settings.docs_dir), blobs, dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"файлов текста:        {о['texts']}\n"
        + f"  залито:             {о['texts_uploaded']}\n"
        + f"  уже было:           {о['texts_already']}\n"
        + f"  пустых (пропущено): {о['texts_empty']}\n"
        + f"ответов OCR:          {о['idp']}\n"
        + f"  залито:             {о['idp_uploaded']}\n"
        + f"  уже было:           {о['idp_already']}\n"
        + "файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
