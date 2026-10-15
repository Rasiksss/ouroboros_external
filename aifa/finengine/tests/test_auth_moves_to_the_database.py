"""Учётные записи переезжают в базу, и переезд не запирает людей снаружи.

🔴 ЗАЧЕМ (решение владельца 25.08.2026: «Постгрес есть нормальный для хранения
всего? Если нет то сделай»). На стенде в Postgres лежала ОДНА таблица
`station_run` на 62 строки; всё остальное — файлами в томе: диалоги (перенесены
в тот же день), `users.db`, `scenarios.db`, `news.db`.

Учётки — особый случай среди них. Потеря диалога это «пропал чат»; потеря
учёток это «никто не может войти», и чинить придётся снаружи. Поэтому здесь
проверяется не только «перенеслось», но и три свойства, без которых переезд
опаснее, чем его отсутствие.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from finengine.auth.db import Base, Invite, User
from finengine.store.migrate_auth import migrate


def _sqlite(tmp_path, имя="users.db") -> str:
    return f"sqlite:///{tmp_path / имя}"


def _завести(url: str, пользователи: list[tuple[int, str]], коды: list[str] = ()) -> None:
    e = create_engine(url, future=True)
    Base.metadata.create_all(e)
    S = sessionmaker(bind=e, future=True)
    with S() as s:
        for uid, имя in пользователи:
            s.add(User(id=uid, username=имя, password_hash=f"хэш-{имя}",
                       is_admin=False, created_at=datetime.now(timezone.utc)))
        s.commit()
        for код in коды:
            s.add(Invite(code=код, created_by_id=пользователи[0][0],
                         created_at=datetime.now(timezone.utc)))
        s.commit()


def _прочитать(url: str):
    e = create_engine(url, future=True)
    S = sessionmaker(bind=e, future=True)
    with S() as s:
        return ({u.username: u.id for u in s.execute(select(User)).scalars()},
                {i.code: i.created_by_id for i in s.execute(select(Invite)).scalars()})


def test_users_and_invites_reach_the_database(tmp_path):
    """Базовое: перенеслись и те, и другие."""
    ист, цель = _sqlite(tmp_path), _sqlite(tmp_path, "target.db")
    _завести(ист, [(1, "admin"), (2, "nate")], ["ПРИГЛАШЕНИЕ-1"])

    о = migrate(ист, цель)

    assert о["users_migrated"] == 2 and о["invites_migrated"] == 1
    имена, коды = _прочитать(цель)
    assert set(имена) == {"admin", "nate"}
    assert "ПРИГЛАШЕНИЕ-1" in коды


def test_identifiers_survive_so_invites_keep_pointing_at_real_people(tmp_path):
    """id сохраняются — иначе ссылки приглашений укажут на ЧУЖИХ.

    Приглашение помнит, кто его выписал (`created_by_id`). Перенеси
    пользователей с новыми id — и приглашение начнёт числиться за другим
    человеком. В аудите это выглядит как выдача доступа не тем.
    """
    ист, цель = _sqlite(tmp_path), _sqlite(tmp_path, "target.db")
    _завести(ист, [(7, "admin"), (9, "nate")], ["К-7"])

    migrate(ист, цель)

    имена, коды = _прочитать(цель)
    assert имена["admin"] == 7 and имена["nate"] == 9
    assert коды["К-7"] == 7, "приглашение перестало указывать на своего автора"


def test_a_second_run_does_not_overwrite_a_changed_password(tmp_path):
    """Идемпотентность с зубами: повтор не откатывает смену пароля.

    Переезд запускают минимум дважды — сухим прогоном и настоящим, а часто и
    третий раз, когда на базе уже поработали. Если бы скрипт лил файл поверх
    базы, второй запуск вернул бы старый хэш, и человек, только что сменивший
    пароль, войти бы не смог.
    """
    ист, цель = _sqlite(tmp_path), _sqlite(tmp_path, "target.db")
    _завести(ист, [(1, "admin")])
    migrate(ист, цель)

    e = create_engine(цель, future=True)
    with sessionmaker(bind=e, future=True)() as s:
        u = s.execute(select(User)).scalars().one()
        u.password_hash = "новый-хэш-после-смены"
        s.commit()

    о = migrate(ист, цель)

    assert о["users_migrated"] == 0 and о["users_already"] == 1
    with sessionmaker(bind=e, future=True)() as s:
        assert s.execute(select(User)).scalars().one().password_hash == "новый-хэш-после-смены"


def test_a_missing_file_is_not_a_crash(tmp_path):
    """Свежая установка: файла учёток нет. Это законно, а не отказ."""
    о = migrate(_sqlite(tmp_path, "нет-такого.db"), _sqlite(tmp_path, "target.db"))
    assert о["users_in_file"] == 0 and о["users_migrated"] == 0


def test_the_column_patch_does_not_speak_sqlite_only():
    """`init_db` больше не зовёт PRAGMA — иначе он ронял бы старт на Postgres.

    Это не придирка к стилю: `PRAGMA table_info` есть ТОЛЬКО у SQLite. Оставь
    его — и смена адреса базы валила бы приложение синтаксической ошибкой на
    старте, то есть переключение не переносило бы вход, а ломало его.
    """
    from pathlib import Path
    исходник = Path(__file__).resolve().parents[1] / "src" / "finengine" / "auth" / "db.py"
    текст = исходник.read_text(encoding="utf-8")
    строки = [s for s in текст.splitlines()
              if "PRAGMA" in s and not s.lstrip().startswith("#")]
    assert not строки, f"в auth/db.py остался SQLite-только PRAGMA: {строки}"


def test_the_default_stays_sqlite_until_someone_sets_the_url():
    """Код ничего не включает сам: пустой адрес = файл, как было."""
    from finengine.config import settings
    assert settings.auth_database_url == "", (
        "адрес базы учёток выставлен по умолчанию — переезд живых пользователей "
        "обязан быть решением человека, а не побочным эффектом обновления")
