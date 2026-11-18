"""Перенос сессий агента с тома в базу. Идемпотентен, файлы не трогает.

    docker compose exec backend python -m finengine.store.migrate_sessions --dry-run
    docker compose exec backend python -m finengine.store.migrate_sessions

ВЛАДЕЛЕЦ БЕРЁТСЯ ИЗ ИМЕНИ КАТАЛОГА, А НЕ ИЗ СОДЕРЖИМОГО. На томе владелец и
есть каталог (`data/sessions/<owner_key>/`); поле внутри JSON могло быть не
записано у старых сессий, и тогда история уехала бы к `_global` — то есть
пропала бы из треда пользователя. Каталог знает точно.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ. Скрипт запустят минимум дважды (сухой прогон и
настоящий). Сессия, которая в базе уже есть, не трогается: иначе повторный
запуск откатил бы ходы, сделанные уже на базе. Файлы остаются страховкой.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finengine.config import settings
from finengine.store.sessions import SqlSessionStore


def migrate(корень: Path, target: SqlSessionStore, dry_run: bool = False) -> dict:
    отчёт = {"on_disk": 0, "migrated": 0, "already": 0, "unreadable": 0}
    for каталог in sorted(p for p in корень.iterdir() if p.is_dir()):
        ключ = каталог.name
        for файл in sorted(каталог.glob("*.json")):
            отчёт["on_disk"] += 1
            # Битый файл пропускаем с подсчётом, а не роняем переезд: одна
            # недописанная сессия не должна останавливать остальные.
            try:
                данные = json.loads(файл.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                отчёт["unreadable"] += 1
                continue
            session_id = файл.stem
            if target.load(_как_число(ключ), session_id) is not None:
                отчёт["already"] += 1
                continue
            отчёт["migrated"] += 1
            if not dry_run:
                target.save(_как_число(ключ), session_id, данные)
    return отчёт


def _как_число(ключ: str) -> int | None:
    """Имя каталога → owner_id. `_global` и нечисловые — `None`.

    Обратное к `sessions.owner_key`: тот приводит `owner_id` к имени каталога,
    этот возвращает имя обратно. Пара обязана сходиться, иначе сессии лягут
    под другим владельцем и просто не найдутся.
    """
    return int(ключ) if ключ.isdigit() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос сессий агента с тома в базу")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    args = parser.parse_args(argv)

    цель = (settings.state_database_url or "").strip()
    if not цель:
        # Адрес не печатаем — в нём пароль.
        print("База не настроена: пуст FINENGINE_STATE_DATABASE_URL. Переносить некуда.")
        return 2
    корень = Path(settings.data_dir) / "sessions"
    if not корень.is_dir():
        print(f"Каталога сессий нет: {корень}. Переносить нечего.")
        return 2

    о = migrate(корень, SqlSessionStore(цель), dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"сессий на томе:    {о['on_disk']}\n"
        + f"перенесено:        {о['migrated']}\n"
        + f"уже было в базе:   {о['already']}\n"
        + f"не прочитались:    {о['unreadable']}\n"
        + "файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
