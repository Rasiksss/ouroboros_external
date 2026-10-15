"""Завести схему контура в Postgres. Осознанный шаг перед переездом контура.

    python -m finengine.store.ensure_schema            # что сделает
    python -m finengine.store.ensure_schema --apply    # сделает

Зачем отдельно, а не при сборке движка: `движок()` не подключается к базе, и
это намеренно — иначе импорт модуля стал бы зависеть от того, поднят ли
Postgres. Схема заводится один раз на контур, руками, как и переезд.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from finengine.config import settings
from finengine.store.engine import движок, проверить_имя, схема, это_постгрес

# Адреса всех хранилищ: схема нужна в той базе, куда контур реально ходит.
_АДРЕСА = (
    "artifacts_database_url", "auth_database_url", "dialogs_database_url",
    "documents_database_url", "journals_database_url", "news_database_url",
    "runs_database_url", "scenarios_database_url", "state_database_url",
)


def базы() -> list[str]:
    """Разные адреса Postgres среди настроенных. Одна база — один заход."""
    собрано: list[str] = []
    for имя in _АДРЕСА:
        адрес = (getattr(settings, имя, "") or "").strip()
        if адрес and это_постгрес(адрес) and адрес not in собрано:
            собрано.append(адрес)
    return собрано


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__)
    разбор.add_argument("--apply", action="store_true",
                        help="выполнить; без него только показывает")
    дов = разбор.parse_args()

    имя = схема()
    if not имя:
        print("FINENGINE_PG_SCHEMA пуста — контур не разведён, схема не нужна.")
        return 0
    проверить_имя(имя)

    адреса = базы()
    if not адреса:
        print("ни одно хранилище не настроено на Postgres — заводить нечего.")
        return 0

    print(f"схема контура: {имя}")
    for адрес in адреса:
        # Адрес не печатаем целиком — в нём пароль.
        хвост = адрес.rsplit("/", 1)[-1]
        if not дов.apply:
            print(f"  [сухой прогон] CREATE SCHEMA IF NOT EXISTS \"{имя}\" в базе .../{хвост}")
            continue
        # Той же дверью: `search_path` на ещё не заведённую схему Postgres
        # принимает молча, а `CREATE SCHEMA` называет её явно.
        дв = движок(адрес, pool_pre_ping=True, future=True, echo=False)
        with дв.begin() as соединение:
            соединение.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{имя}"'))
        дв.dispose()
        print(f"  ✅ .../{хвост}")

    if not дов.apply:
        print("\nничего не сделано. Повторить с --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
