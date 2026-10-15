"""Перенос учётных записей из SQLite в базу. Идемпотентен, файл не трогает.

Запускать там, где лежат и код, и том с данными, — в контейнере бэкенда:

    docker compose exec backend python -m finengine.store.migrate_auth --dry-run
    docker compose exec backend python -m finengine.store.migrate_auth

🔴 ПОЧЕМУ ОТДЕЛЬНЫМ ШАГОМ, А НЕ АВТОМАТОМ ПРИ СТАРТЕ. Учётные записи —
единственные данные, потеря которых запирает пользователей СНАРУЖИ: это не
«пропал диалог», а «никто не может войти». Поэтому переезд — осознанное
действие человека: сначала сухой прогон, потом настоящий, и только потом
переключение адреса. Файл `users.db` остаётся на диске страховкой; откат — это
снятие `FINENGINE_AUTH_DATABASE_URL` и рестарт.

ДОПОЛНЯЕМ, А НЕ ПЕРЕЗАПИСЫВАЕМ. Скрипт почти наверняка запустят не один раз.
Если бы он лил файл поверх базы, второй запуск затирал бы пароли, сменённые
после первого, — ровно та потеря, ради ухода от которой всё и затевается.
Поэтому: пользователь, который в базе уже есть (по `username`), не трогается;
переносятся только отсутствующие. То же для приглашений (по `code`).

ПОРЯДОК ВАЖЕН: сперва пользователи, потом приглашения. У приглашения есть
ссылки `created_by_id` / `used_by_id`; вставить его раньше, чем появится
пользователь, значит получить битую ссылку. Идентификаторы при этом
СОХРАНЯЮТСЯ — иначе ссылки указали бы на чужие строки.
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from finengine.auth.db import Base, Invite, User
from finengine.config import settings
from finengine.store.engine import движок

log = logging.getLogger(__name__)


def _sqlite_url() -> str:
    return f"sqlite:///{settings.auth_db_path.resolve()}"


def migrate(source_url: str, target_url: str, dry_run: bool = False) -> dict:
    """Перенести users + invites из `source_url` в `target_url`.

    Возвращает счётчики. Ничего не удаляет и не переписывает.
    """
    источник = движок(source_url, future=True, echo=False)
    приёмник = движок(target_url, pool_pre_ping=True, future=True, echo=False)

    отчёт = {"users_in_file": 0, "users_migrated": 0, "users_already": 0,
             "invites_in_file": 0, "invites_migrated": 0, "invites_already": 0}

    # Файла может не быть вовсе — это законно (свежая установка), и падать тут
    # незачем: переносить просто нечего.
    if not inspect(источник).has_table("users"):
        return отчёт

    Base.metadata.create_all(приёмник)
    Src = sessionmaker(bind=источник, future=True)
    Dst = sessionmaker(bind=приёмник, future=True, expire_on_commit=False)

    with Src() as src, Dst() as dst:
        пользователи = list(src.execute(select(User)).scalars())
        отчёт["users_in_file"] = len(пользователи)
        есть_имена = {u.username for u in dst.execute(select(User)).scalars()}
        for u in пользователи:
            if u.username in есть_имена:
                отчёт["users_already"] += 1
                continue
            отчёт["users_migrated"] += 1
            if dry_run:
                continue
            # Идентификатор сохраняем: на него ссылаются приглашения.
            dst.add(User(**{c.name: getattr(u, c.name) for c in User.__table__.columns}))
        if not dry_run:
            dst.commit()

        приглашения = list(src.execute(select(Invite)).scalars())
        отчёт["invites_in_file"] = len(приглашения)
        есть_коды = {i.code for i in dst.execute(select(Invite)).scalars()}
        for i in приглашения:
            if i.code in есть_коды:
                отчёт["invites_already"] += 1
                continue
            отчёт["invites_migrated"] += 1
            if dry_run:
                continue
            dst.add(Invite(**{c.name: getattr(i, c.name) for c in Invite.__table__.columns}))
        if not dry_run:
            dst.commit()

    return отчёт


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос учётных записей из SQLite в базу")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, ничего не писать")
    args = parser.parse_args(argv)

    цель = (settings.auth_database_url or "").strip()
    if not цель:
        # Адрес не печатаем ни в каком виде — в нём пароль.
        print("База не настроена: пуст FINENGINE_AUTH_DATABASE_URL. Переносить некуда.")
        return 2
    if not settings.auth_db_path.exists():
        print(f"Файла учёток нет: {settings.auth_db_path}. Переносить нечего.")
        return 2

    о = migrate(_sqlite_url(), цель, dry_run=args.dry_run)
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"пользователей в файле:   {о['users_in_file']}\n"
        + f"перенесено пользователей:{о['users_migrated']}\n"
        + f"уже было в базе:         {о['users_already']}\n"
        + f"приглашений в файле:     {о['invites_in_file']}\n"
        + f"перенесено приглашений:  {о['invites_migrated']}\n"
        + f"уже было в базе:         {о['invites_already']}\n"
        + "исходный файл оставлен на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
