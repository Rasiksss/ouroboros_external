"""Перенос файлов источников с тома в объектное хранилище. Идемпотентен.

    docker compose exec backend python -m finengine.store.migrate_sources --dry-run
    docker compose exec backend python -m finengine.store.migrate_sources

РАСКЛАДКА КЛЮЧЕЙ ПОВТОРЯЕТ ФАЙЛОВУЮ ОДИН В ОДИН — `users/<uid>/projects/<pid>/
sources/<имя>`. Поэтому перенос это копирование, а не пересборка: имя объекта
совпадает с прежним относительным путём, и сверять глазами ничего не нужно.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ: объект, который в хранилище уже есть, не
заливаем заново. Файлы на томе остаются страховкой на время переезда.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from finengine.config import settings
from finengine.store.blobs import BlobStore, get_blob_store


def migrate(корень: Path, blobs: BlobStore, dry_run: bool = False) -> dict:
    отчёт = {"on_disk": 0, "uploaded": 0, "already": 0}
    if not корень.is_dir():
        return отчёт
    for файл in sorted(корень.rglob("*")):
        if not файл.is_file():
            continue
        # Ключ — относительный путь от каталога данных: ровно то, чем он был
        # на томе, включая `users/…/sources/`.
        ключ = str(файл.relative_to(корень.parent))
        отчёт["on_disk"] += 1
        if blobs.exists(ключ):
            отчёт["already"] += 1
            continue
        отчёт["uploaded"] += 1
        if not dry_run:
            with open(файл, "rb") as f:
                blobs.put(ключ, f)
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Перенос файлов источников с тома в объектное хранилище")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    blobs = get_blob_store()
    if hasattr(blobs, "ensure_bucket"):
        blobs.ensure_bucket()

    о = migrate(Path(settings.data_dir) / "users", blobs, dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"файлов на томе:       {о['on_disk']}\n"
        + f"залито объектов:      {о['uploaded']}\n"
        + f"уже было в хранилище: {о['already']}\n"
        + "файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
