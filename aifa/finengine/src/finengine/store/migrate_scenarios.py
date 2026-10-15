"""Перенос сценариев из файла SQLite в базу. Идемпотентен, файл не трогает.

    docker compose exec backend python -m finengine.store.migrate_scenarios --dry-run
    docker compose exec backend python -m finengine.store.migrate_scenarios

⚠️ ПЕРЕЕЗД ТРЕБУЕТСЯ НЕ ВСЕГДА. Модуль `skills/scenarios/store.py` переписан на
SQLAlchemy Core и по умолчанию читает ТОТ ЖЕ файл `scenarios.db` — схема
совпадает, переносить ничего не надо. Этот скрипт нужен ровно тогда, когда
хранилище переводят на Postgres (`FINENGINE_SCENARIOS_DATABASE_URL`).

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ. Сценарий, который в базе уже есть (по `id`), не
трогаем: повторный запуск не должен откатывать правку расписания, сделанную уже
на базе. Файл остаётся страховкой.
"""
from __future__ import annotations

import argparse

from sqlalchemy import insert, select

from finengine.config import settings
from finengine.skills.scenarios.store import SCENARIOS, _metadata, db_path
from finengine.store.engine import движок


def migrate(source_url: str, target_url: str, dry_run: bool = False) -> dict:
    источник = движок(source_url, future=True, echo=False)
    приёмник = движок(target_url, pool_pre_ping=True, future=True, echo=False)
    _metadata.create_all(приёмник)

    отчёт = {"in_file": 0, "migrated": 0, "already": 0}
    with источник.begin() as src, приёмник.begin() as dst:
        есть = {r[0] for r in dst.execute(select(SCENARIOS.c.id))}
        for строка in src.execute(select(SCENARIOS)).mappings():
            отчёт["in_file"] += 1
            if строка["id"] in есть:
                отчёт["already"] += 1
                continue
            отчёт["migrated"] += 1
            if not dry_run:
                dst.execute(insert(SCENARIOS).values(**dict(строка)))
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос сценариев из файла в базу")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    цель = (settings.scenarios_database_url or "").strip()
    if not цель:
        # Адрес не печатаем — в нём пароль.
        print("База не настроена: пуст FINENGINE_SCENARIOS_DATABASE_URL. Переносить некуда.")
        return 2
    if not db_path().exists():
        print(f"Файла сценариев нет: {db_path()}. Переносить нечего.")
        return 2

    о = migrate(f"sqlite:///{db_path()}", цель, dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"сценариев в файле: {о['in_file']}\n"
        + f"перенесено:        {о['migrated']}\n"
        + f"уже было в базе:   {о['already']}\n"
        + "исходный файл оставлен на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
