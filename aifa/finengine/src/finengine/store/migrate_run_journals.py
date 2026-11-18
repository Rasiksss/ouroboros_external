"""Перенос журналов прогонов с тома в общее хранилище. Идемпотентен.

    docker compose exec backend python -m finengine.store.migrate_run_journals --dry-run
    docker compose exec backend python -m finengine.store.migrate_run_journals

⚠️ ПЕРЕНОС НЕ ОБЯЗАТЕЛЕН ДЛЯ РАБОТЫ. Прогоны эпохи файлового журнала читаются
откатом на диск (`run_orchestrator._из_журнала`: спросить хранилище, потом
файл). Скрипт нужен, чтобы снять этот откат — то есть чтобы старые прогоны
открывались и с реплики, у которой тома нет вовсе.

ПОВТОРНЫЙ ЗАПУСК НЕ ДУБЛИРУЕТ: прогон, у которого в хранилище уже есть события,
пропускается целиком. Файлы остаются на месте.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finengine.config import settings
from finengine.skills.run_orchestrator import KNOWN_KINDS, вид_журнала
from finengine.store import journals

ВЛАДЕЛЕЦ = "_"


def migrate(docs_dir: Path, dry_run: bool = False) -> dict:
    отчёт = {"runs": 0, "migrated": 0, "already": 0, "events": 0, "bad_lines": 0}
    for kind in KNOWN_KINDS:
        каталоги = [docs_dir / f"{kind}_events"]
        if kind == "chain":
            # Легаси того же вида: старые прогоны цепочек лежат отдельно.
            каталоги.append(docs_dir / "chain_events")
        for d in каталоги:
            if not d.is_dir():
                continue
            for файл in sorted(d.glob("*.jsonl")):
                run_id = файл.stem
                отчёт["runs"] += 1
                if journals.read(вид_журнала(kind), owner=ВЛАДЕЛЕЦ, key=run_id):
                    отчёт["already"] += 1
                    continue
                события = []
                for строка in файл.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not строка.strip():
                        continue
                    # Битую строку считаем и идём дальше: журнал дописывается и
                    # мог оборваться на перезапуске.
                    try:
                        события.append(json.loads(строка))
                    except json.JSONDecodeError:
                        отчёт["bad_lines"] += 1
                if not события:
                    continue
                отчёт["migrated"] += 1
                отчёт["events"] += len(события)
                if not dry_run:
                    for e in события:
                        journals.append(вид_журнала(kind), owner=ВЛАДЕЛЕЦ, key=run_id, entry=e)
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос журналов прогонов в общее хранилище")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    о = migrate(Path(settings.docs_dir), dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"журналов на томе:  {о['runs']}\n"
        + f"перенесено:        {о['migrated']}\n"
        + f"уже было:          {о['already']}\n"
        + f"событий:           {о['events']}\n"
        + f"битых строк:       {о['bad_lines']}\n"
        + "файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
