"""Перенос новостного конвейера из файла SQLite в базу. Идемпотентен.

    docker compose exec backend python -m finengine.store.migrate_news --dry-run
    docker compose exec backend python -m finengine.store.migrate_news

⚠️ ПЕРЕЕЗД ТРЕБУЕТСЯ НЕ ВСЕГДА. `skills/news/db.py` переписан на SQLAlchemy
Core и по умолчанию читает ТОТ ЖЕ файл `news.db` — схема совпадает, переносить
ничего не надо. Скрипт нужен ровно тогда, когда хранилище переводят на Postgres
(`FINENGINE_NEWS_DATABASE_URL`).

⚠️ ОБЪЁМ. На стенде `raw_items` — 235 тысяч строк и почти все 174 МБ базы.
Перенос идёт ПАЧКАМИ, а не одной транзакцией: одна транзакция на четверть
миллиона строк держит блокировку минутами и на отказе откатывает всё.

ПОРЯДОК ВАЖЕН: источники → сырьё → прогоны → пункты. У пунктов ссылки на прогон
и на сырьё; перенеси их раньше — получишь битые ссылки.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ: строка, которая в базе уже есть, пропускается.
"""
from __future__ import annotations

import argparse

from sqlalchemy import insert, select

from finengine.config import settings
from finengine.skills.news.db import (
    DIGEST_ITEMS, DIGEST_RUNS, RAW_ITEMS, SOURCES, _metadata, db_path,
)
from finengine.store.engine import движок

ПАЧКА = 2000


def _перенести(src, dst, таблица, ключ_имя: str, dry_run: bool) -> tuple[int, int]:
    """Перенести одну таблицу пачками. Возвращает (было, перенесено).

    🔴 ЧИТАЕМ ТОЛЬКО ТЕ КОЛОНКИ, КОТОРЫЕ ЕСТЬ В ИСТОЧНИКЕ (поймано тестом).
    `user_id` и `diag` в `digest_runs` дописывались ALTER-ом на живой базе, и
    файл, снятый раньше, приходит без них. Выбери всю таблицу — SQLAlchemy
    построит SELECT с несуществующей колонкой и переезд упадёт на ровном месте.

    Это тот же класс, что уронил переезд документов: старая запись не обязана
    знать про поля, заведённые после неё.
    """
    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

    в_источнике = {c["name"] for c in sa_inspect(src).get_columns(таблица.name)}
    колонки = [c for c in таблица.c if c.name in в_источнике]
    стлб = таблица.c[ключ_имя]
    есть = {r[0] for r in dst.execute(select(стлб))}
    было = перенесено = 0
    буфер: list[dict] = []
    for строка in src.execute(select(*колонки)).mappings():
        было += 1
        if строка[ключ_имя] in есть:
            continue
        перенесено += 1
        if dry_run:
            continue
        буфер.append(dict(строка))
        if len(буфер) >= ПАЧКА:
            dst.execute(insert(таблица), буфер)
            буфер.clear()
    if буфер and not dry_run:
        dst.execute(insert(таблица), буфер)
    return было, перенесено


def migrate(source_url: str, target_url: str, dry_run: bool = False) -> dict:
    источник = движок(source_url, future=True, echo=False)
    приёмник = движок(target_url, pool_pre_ping=True, future=True, echo=False)
    _metadata.create_all(приёмник)

    отчёт: dict[str, int] = {}
    # Порядок — по ссылкам: источники → сырьё → прогоны → пункты.
    план = ((SOURCES, "source_id"), (RAW_ITEMS, "raw_hash"),
            (DIGEST_RUNS, "run_id"), (DIGEST_ITEMS, "id"))
    with источник.connect() as src, приёмник.begin() as dst:
        for таблица, ключ in план:
            было, перенесено = _перенести(src, dst, таблица, ключ, dry_run)
            отчёт[f"{таблица.name}_было"] = было
            отчёт[f"{таблица.name}_перенесено"] = перенесено
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос новостей из файла в базу")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    цель = (settings.news_database_url or "").strip()
    if not цель:
        # Адрес не печатаем — в нём пароль.
        print("База не настроена: пуст FINENGINE_NEWS_DATABASE_URL. Переносить некуда.")
        return 2
    if not db_path().exists():
        print(f"Файла новостей нет: {db_path()}. Переносить нечего.")
        return 2

    о = migrate(f"sqlite:///{db_path()}", цель, dry_run=args.dry_run)
    if args.dry_run:
        print("СУХОЙ ПРОГОН — ничего не записано")
    for имя in ("sources", "raw_items", "digest_runs", "digest_items"):
        print(f"{имя:14} в файле {о[f'{имя}_было']:>7}, перенесено {о[f'{имя}_перенесено']:>7}")
    print("исходный файл оставлен на месте")
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
