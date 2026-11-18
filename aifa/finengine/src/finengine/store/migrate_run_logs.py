"""Перенос журналов прогонов с тома в общее хранилище. Идемпотентен.

    docker compose exec backend python -m finengine.store.migrate_run_logs --dry-run
    docker compose exec backend python -m finengine.store.migrate_run_logs

⚠️ ПЕРЕНОС НЕ ОБЯЗАТЕЛЕН ДЛЯ РАБОТЫ: `run_logger.прочитать` откатывается на
старый файл, когда в хранилище пусто. Скрипт снимает этот откат — то есть
делает старые записи видимыми с реплики, у которой тома нет вовсе.

⚠️ СЫРЬЁ СТАНЦИИ РАСКЛАДЫВАЕТСЯ ПО НОНСАМ. На томе это был один файл на всё, и
чтобы посмотреть один ход, его перебирали целиком. В хранилище ключ — нонс, и
разбор одного хода становится одним чтением.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finengine.config import settings
from finengine.skills.run_logger import (
    _ОБЩИЙ, ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, ЖУРНАЛ_КЛИЕНТА, ЖУРНАЛ_РАЗБОРОВ, ЖУРНАЛ_ЧАТА,
    СЫРЬЁ_СТАНЦИИ,
)
from finengine.store import journals

ВЛАДЕЛЕЦ = "_"

# файл → (вид журнала, ключ или None — тогда ключ берётся из поля `nonce`)
ФАЙЛЫ = (
    ("extract_runs.jsonl", ЖУРНАЛ_ИЗВЛЕЧЕНИЙ, _ОБЩИЙ),
    ("chat_runs.jsonl", ЖУРНАЛ_ЧАТА, _ОБЩИЙ),
    ("analysis_runs.jsonl", ЖУРНАЛ_РАЗБОРОВ, _ОБЩИЙ),
    ("station_events.jsonl", СЫРЬЁ_СТАНЦИИ, None),
    ("client_events.jsonl", ЖУРНАЛ_КЛИЕНТА, _ОБЩИЙ),
)


def migrate(docs_dir: Path, dry_run: bool = False) -> dict:
    отчёт: dict = {}
    for имя, вид, ключ in ФАЙЛЫ:
        файл = docs_dir / имя
        счёт = {"lines": 0, "migrated": 0, "already": 0, "bad_lines": 0}
        отчёт[имя] = счёт
        if not файл.is_file():
            continue
        # Повторный запуск не дублирует: непустой журнал пропускаем целиком.
        # У сырья станции ключей много, поэтому проверяем по первому нонсу.
        строки = []
        for строка in файл.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not строка.strip():
                continue
            счёт["lines"] += 1
            try:
                строки.append(json.loads(строка))
            except json.JSONDecodeError:
                счёт["bad_lines"] += 1
        if not строки:
            continue
        первый_ключ = ключ or str(строки[0].get("nonce") or "")
        if первый_ключ and journals.read(вид, owner=ВЛАДЕЛЕЦ, key=первый_ключ):
            счёт["already"] = len(строки)
            continue
        for запись in строки:
            k = ключ or str(запись.get("nonce") or "")
            if not k:
                счёт["bad_lines"] += 1
                continue
            счёт["migrated"] += 1
            if not dry_run:
                journals.append(вид, owner=ВЛАДЕЛЕЦ, key=k, entry=запись)
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос журналов прогонов в хранилище")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    о = migrate(Path(settings.docs_dir), dry_run=args.dry_run)
    if args.dry_run:
        print("СУХОЙ ПРОГОН — ничего не записано")
    for имя, с in о.items():
        print(f"{имя:24} строк {с['lines']:6}  перенесено {с['migrated']:6}  "
              f"уже было {с['already']:6}  битых {с['bad_lines']}")
    print("файлы оставлены на месте")
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
