"""Перенос личного состояния пользователя в общее хранилище. Идемпотентен.

    docker compose exec backend python -m finengine.store.migrate_user_state --dry-run
    docker compose exec backend python -m finengine.store.migrate_user_state

ЧТО ПЕРЕЕЗЖАЕТ (старое место → новое):

    users/<uid>/projects/<pid>/watchlist.json → (watchlist,   uid, pid)
    users/<uid>/sources.json                  → (user_state,  uid, sources)
    users/<uid>/methodology.json              → (methodology, uid, overlay)

🔴 ЭТОТ СКРИПТ ПОЯВИЛСЯ ПОТОМУ, ЧТО Я ЕГО ЗАБЫЛ, И ЗАБЫЛ ХАРАКТЕРНО. Модули
перевели на `store/state`, тумблер включили — а раскладка у нового хранилища
СВОЯ (`<вид>/<владелец>/<ключ>.json`), и старые файлы под неё не попадают.
Отказа при этом нет ни одного: список наблюдения молча пересевается из
встроенного реестра, личная методика читается пустой, правка каталога новостей
сбрасывается. То есть пользователь видит не ошибку, а «мои настройки
сбросились» — ровно та недоставка, которую мы ловим весь месяц.

Отсюда правило, которое дороже самого скрипта: ПЕРЕВЁЛ ЧИТАТЕЛЯ — ПРОВЕРЬ, ЧТО
СТАРЫЕ ДАННЫЕ ПОПАДАЮТ В НОВОЕ МЕСТО. Совпадения имён недостаточно, нужна
раскладка.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ: что уже лежит в хранилище, не трогаем — иначе
повторный запуск откатил бы правки, сделанные уже после переезда.

⚠️ ЗНАНИЯ ЧАТА (`users/<uid>/chats/<id>/knowledge/`) СЮДА НЕ ВХОДЯТ: база
знаний чата снесена 07.09.2026 вместе с модулем, каталоги на томе — остатки.
Сказано вслух, чтобы никто не решил, что каталог пользователя переехал целиком.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finengine.config import settings
from finengine.store import state


def migrate(корень: Path, store: state.StateStore, dry_run: bool = False) -> dict:
    отчёт = {"found": 0, "migrated": 0, "already": 0, "unreadable": 0}
    if not корень.is_dir():
        return отчёт

    for каталог in sorted(p for p in корень.iterdir() if p.is_dir()):
        владелец = state.safe_segment(каталог.name)
        предметы: list[tuple[str, str, Path]] = [
            (state.USER_STATE, "sources", каталог / "sources.json"),
            (state.METHODOLOGY, "overlay", каталог / "methodology.json"),
        ]
        проекты = каталог / "projects"
        if проекты.is_dir():
            предметы += [
                (state.WATCHLIST, state.safe_segment(p.name), p / "watchlist.json")
                for p in sorted(x for x in проекты.iterdir() if x.is_dir())
            ]

        for вид, ключ, файл in предметы:
            if not файл.is_file():
                continue
            отчёт["found"] += 1
            if store.get(вид, владелец, ключ) is not None:
                отчёт["already"] += 1
                continue
            # Битый файл считаем и идём дальше: одна испорченная настройка не
            # должна останавливать перенос остальных.
            try:
                документ = json.loads(файл.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                отчёт["unreadable"] += 1
                continue
            if not isinstance(документ, dict):
                отчёт["unreadable"] += 1
                continue
            отчёт["migrated"] += 1
            if not dry_run:
                store.put(вид, владелец, ключ, документ)
    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Перенос личного состояния пользователя в общее хранилище")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    о = migrate(Path(settings.data_dir) / "users", state.get_state_store(),
                dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"найдено файлов:    {о['found']}\n"
        + f"перенесено:        {о['migrated']}\n"
        + f"уже было:          {о['already']}\n"
        + f"не прочитались:    {о['unreadable']}\n"
        + "файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
