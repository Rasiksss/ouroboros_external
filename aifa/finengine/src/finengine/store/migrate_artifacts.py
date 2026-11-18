"""Перенос артефактов проектов с тома в базу. Идемпотентен, файлы не трогает.

    docker compose exec backend python -m finengine.store.migrate_artifacts --dry-run
    docker compose exec backend python -m finengine.store.migrate_artifacts

ВЛАДЕЛЕЦ И ПРОЕКТ БЕРУТСЯ ИЗ ПУТИ, а не из карточки: на томе путь и есть
единственное место, где они записаны, — в `meta.json` их нет вовсе.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ: артефакт, который в базе уже есть, не трогаем.
Файлы остаются страховкой на время переезда.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finengine.config import settings
from finengine.store.artifacts import ArtifactMeta, SqlArtifactStore


def migrate(корень: Path, target: SqlArtifactStore, dry_run: bool = False) -> dict:
    отчёт = {"on_disk": 0, "migrated": 0, "already": 0, "unreadable": 0}
    for каталог_владельца in sorted(p for p in корень.iterdir() if p.is_dir()):
        for каталог_проекта in sorted(p for p in каталог_владельца.iterdir() if p.is_dir()):
            владелец, проект = каталог_владельца.name, каталог_проекта.name
            for прогон in sorted(p for p in каталог_проекта.iterdir() if p.is_dir()):
                отчёт["on_disk"] += 1
                карточка, нагрузка = прогон / "meta.json", прогон / "result.json"
                # Битый артефакт пропускаем с подсчётом: один недописанный
                # прогон не должен останавливать перенос остальных.
                try:
                    meta = ArtifactMeta(**json.loads(карточка.read_text(encoding="utf-8")))
                    payload = json.loads(нагрузка.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    отчёт["unreadable"] += 1
                    continue
                if target.load(владелец, проект, meta.run_id) is not None:
                    отчёт["already"] += 1
                    continue
                отчёт["migrated"] += 1
                if not dry_run:
                    target.save(владелец, проект, meta, payload)
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос артефактов проектов с тома в базу")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    цель = (settings.artifacts_database_url or "").strip()
    if not цель:
        # Адрес не печатаем — в нём пароль.
        print("База не настроена: пуст FINENGINE_ARTIFACTS_DATABASE_URL. Переносить некуда.")
        return 2
    корень = Path(settings.data_dir) / "artifacts"
    if not корень.is_dir():
        print(f"Каталога артефактов нет: {корень}. Переносить нечего.")
        return 2

    о = migrate(корень, SqlArtifactStore(цель), dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"артефактов на томе: {о['on_disk']}\n"
        + f"перенесено:         {о['migrated']}\n"
        + f"уже было в базе:    {о['already']}\n"
        + f"не прочитались:     {о['unreadable']}\n"
        + "файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
