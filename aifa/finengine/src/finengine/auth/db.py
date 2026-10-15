"""Модели и сессия учёток. Только Postgres — файлового пути нет с 26.08.2026."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Boolean, ForeignKey, DateTime
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

from finengine.config import settings
from finengine.store.engine import движок


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Comma-separated keys of premium models the user is allowed to switch to
    # (e.g. "kimi,glm"). The base model is always available — gating only
    # controls extras. Set on registration from the invite that the user used.
    allowed_models: Mapped[str] = mapped_column(String(255), default="")


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    used_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Comma-separated premium model keys to grant when this invite is redeemed.
    allowed_models: Mapped[str] = mapped_column(String(255), default="")

    created_by: Mapped["User"] = relationship(foreign_keys=[created_by_id])
    used_by: Mapped[Optional["User"]] = relationship(foreign_keys=[used_by_id])


# Engine + session. Адрес `FINENGINE_AUTH_DATABASE_URL` ОБЯЗАТЕЛЕН.
#
# Учётные записи — единственные данные, потеря которых запирает пользователей
# снаружи: не «пропал диалог», а «никто не может войти». Переезд со старого
# файла — отдельный осознанный шаг (`python -m finengine.store.migrate_auth`);
# старый файл остаётся на диске страховкой.
# 🔴 ФАЙЛОВЫЙ ЗАПАСНОЙ ПУТЬ СНЕСЁН 26.08.2026. Здесь была ветка
# `sqlite:///users.db` на случай ненастроенного адреса. Она давала худший из
# исходов: приложение поднималось «как будто получилось», заводило пустую базу
# учёток и запирало снаружи ВСЕХ — не «пропал диалог», а «никто не может войти».
# Теперь адрес обязателен, и отказ громкий.
#
# Движок собирается ЛЕНИВО. Модуль держит и ORM-классы (`User`, `Invite`), их
# импортируют полтора десятка мест ради типов; собирать движок на импорте
# значило бы требовать поднятый Postgres от всякого, кто просто назвал тип.
_движок: tuple[str, Engine, "sessionmaker"] | None = None


def _собранное() -> tuple[Engine, "sessionmaker"]:
    """Движок и фабрика сессий базы учёток. Адрес ОБЯЗАТЕЛЕН, запасного нет.

    В ключе кэша и схема контура: она живёт внутри движка (store/engine.py),
    и по одному адресу с двумя схемами кэш отдавал бы движок чужого контура.
    Фабрика сессий кэшируется ВМЕСТЕ с движком — собирать sessionmaker на
    каждый запрос значило бы платить за конструктор там, где ничего не меняется.
    """
    global _движок
    url = (settings.auth_database_url or "").strip()
    from finengine.store.engine import схема  # noqa: PLC0415 — разрыв цикла: engine не должен тянуть auth

    ключ = f"{url}|{схема()}"
    if _движок is not None and _движок[0] == ключ:
        return _движок[1], _движок[2]
    if not url:
        raise RuntimeError(
            "база учёток не настроена: пуст FINENGINE_AUTH_DATABASE_URL. "
            "Файлового запасного пути больше нет (снесён 26.08.2026)")
    # echo=False намеренно: в строке подключения пароль, а с echo=True
    # SQLAlchemy печатает её в лог.
    дв = движок(url, pool_pre_ping=True, future=True, echo=False)
    фабрика = sessionmaker(bind=дв, autoflush=False, expire_on_commit=False)
    _движок = (ключ, дв, фабрика)
    return дв, фабрика


def движок_учёток() -> Engine:
    return _собранное()[0]


def SessionLocal() -> Session:  # noqa: N802 — имя сохранено: его зовут в 4 местах
    """Сессия к базе учёток. Движок поднимается при первом обращении."""
    return _собранное()[1]()


def init_db() -> None:
    engine = движок_учёток()
    Base.metadata.create_all(engine)
    # Дописать колонки, заведённые после того, как база уже развернулась:
    # `create_all` создаёт таблицы, но не делает ALTER, а база переживает
    # пересборку образа.
    from sqlalchemy import inspect as _inspect, text

    инспектор = _inspect(engine)
    таблицы = set(инспектор.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in (
            ("users", "allowed_models", "ALTER TABLE users ADD COLUMN allowed_models VARCHAR(255) DEFAULT ''"),
            ("invites", "allowed_models", "ALTER TABLE invites ADD COLUMN allowed_models VARCHAR(255) DEFAULT ''"),
        ):
            if table not in таблицы:
                continue
            есть = {c["name"] for c in инспектор.get_columns(table)}
            if column not in есть:
                conn.execute(text(ddl))


def get_session() -> Session:
    """FastAPI dependency. Closes session after the request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
