"""Перенос журналов с тома в базу. Идемпотентен, файлы не трогает.

    docker compose exec backend python -m finengine.store.migrate_journals --dry-run
    docker compose exec backend python -m finengine.store.migrate_journals

⚠️ СЛЕДЫ — САМЫЙ ТЯЖЁЛЫЙ ЖУРНАЛ (на стенде 39 МБ против 1,2 МБ у двух других
вместе): в них ложатся полные полезные нагрузки без урезания. Сухой прогон
покажет счётчики до того, как это поедет в базу.

ПОВТОРНЫЙ ЗАПУСК НЕ ДУБЛИРУЕТ. У журнала нет своего идентификатора записи,
поэтому «уже перенесено» решается по паре (владелец, ключ): если в базе по ней
уже есть записи, файл пропускается целиком. Значит дописанное в файл ПОСЛЕ
первого переезда вторым запуском не подхватится — но после переезда в файл уже
никто не пишет, туда пишет база.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finengine.config import settings
from finengine.store.journals import AUDIT, LLM_TRACES, TRACES, SqlJournalStore

ВИДЫ = (TRACES, AUDIT, LLM_TRACES)


def migrate(корень: Path, target: SqlJournalStore, dry_run: bool = False) -> dict:
    отчёт: dict = {"files": 0, "lines": 0, "migrated": 0, "skipped_files": 0, "bad_lines": 0}
    for вид in ВИДЫ:
        каталог_вида = корень / вид
        if not каталог_вида.is_dir():
            continue
        for каталог_владельца in sorted(p for p in каталог_вида.iterdir() if p.is_dir()):
            владелец = каталог_владельца.name
            for файл in sorted(каталог_владельца.glob("*.jsonl")):
                отчёт["files"] += 1
                ключ = файл.stem
                if target.read(вид, владелец, ключ):
                    отчёт["skipped_files"] += 1
                    continue
                for строка in файл.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not строка.strip():
                        continue
                    отчёт["lines"] += 1
                    # Битую строку считаем и идём дальше: журнал дописывается и
                    # мог оборваться на перезапуске.
                    try:
                        запись = json.loads(строка)
                    except json.JSONDecodeError:
                        отчёт["bad_lines"] += 1
                        continue
                    отчёт["migrated"] += 1
                    if not dry_run:
                        target.append(вид, владелец, ключ, запись)
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос журналов с тома в базу")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    цель = (settings.journals_database_url or "").strip()
    if not цель:
        # Адрес не печатаем — в нём пароль.
        print("База не настроена: пуст FINENGINE_JOURNALS_DATABASE_URL. Переносить некуда.")
        return 2

    о = migrate(Path(settings.data_dir), SqlJournalStore(цель), dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"файлов журналов:   {о['files']}\n"
        + f"пропущено (есть):  {о['skipped_files']}\n"
        + f"строк прочитано:   {о['lines']}\n"
        + f"перенесено:        {о['migrated']}\n"
        + f"битых строк:       {о['bad_lines']}\n"
        + "файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
