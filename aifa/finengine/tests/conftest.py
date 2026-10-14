"""Общая уборка между тестами.

🔴 ЗАЧЕМ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Пока координация хода жила словарями в памяти
модулей, каждый тест чистил их сам — и это работало, потому что чистить было
дёшево и очевидно. После переезда на общую шину и общее состояние (правило 3.2:
реплик может быть больше одной) забытая уборка перестала быть безобидной:
подписка одного теста доживает до следующего, и тот видит подписчика там, где
проверяет ЕГО ОТСУТСТВИЕ.

Так упали два сторожа: приём прогресса станции отвечал `delivered_elsewhere`
вместо `no_consumer` — то есть тест, проверяющий потерю событий 18.08, больше
не проверял её вовсе. Отказа при этом не было: тест просто перестал ловить.

Поэтому уборка здесь, а не в каждом файле: место одно, забыть его нельзя.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
_ENGINE_SCAN_ROOTS = (ROOT / "src" / "finengine", ROOT / "station-skills")


@pytest.fixture(scope="session")
def движок_ast() -> dict[Path, ast.Module]:
    """AST каждого .py-файла движка, распарсенный РОВНО ОДИН РАЗ за сессию.

    ЗАЧЕМ. Пять гвард-тестов (`test_registry_external_dir.py`,
    `test_bg_extract_no_duplicate.py`, `test_document_text_lives_in_one_place.py`,
    `test_calculated_rows_all_carry_inputs.py`,
    `test_station_has_what_the_code_imports.py`) независимо делали
    `rglob("*.py") + ast.parse()` по одному и тому же/вложенному дереву —
    измерено `pytest --durations`: ~3,3с из 71с прогона тратились на
    ПОВТОРНЫЙ парсинг одних и тех же файлов (13.09.2026).

    Состав файлов под каждым корнем зафиксирован
    `tests/test_the_engine_scan_is_not_silently_narrowed.py`: если этот
    словарь тихо сузится (например, забудут один из `_ENGINE_SCAN_ROOTS`),
    тот сторож покраснеет первым — раньше, чем гварды, которые сюда переедут.

    Потребитель со своим (под)корнем фильтрует результат сам, без нового
    прохода по диску:

        {p: t for p, t in движок_ast.items() if root in p.parents}
    """
    return {
        p: ast.parse(p.read_text(encoding="utf-8"))
        for root in _ENGINE_SCAN_ROOTS
        for p in sorted(root.rglob("*.py"))
    }


@pytest.fixture(autouse=True)
def _чистая_координация():
    """Шина и её подписки не переживают тест.

    Чистим ДО и ПОСЛЕ: «до» защищает от теста, который упал и не прибрал за
    собой, «после» — от следующего.
    """
    from finengine.api import internal_tools as _it
    from finengine.api import station_progress_ingest as _spi
    from finengine.store import bus as _bus

    def _прибрать() -> None:
        _bus.reset_bus()
        _spi._bus.clear()
        _spi._подписки.clear()
        # Контекст хода тоже переехал в общее состояние: без уборки отметка
        # вызванных тулов копится через весь набор.
        _it.reset_sessions()

    _прибрать()
    yield
    _прибрать()


# ---------------------------------------------------------------------------
# Реальный Postgres для тестов хранилищ
# ---------------------------------------------------------------------------
#
# 🔴 ЗАЧЕМ. Правило Алексея 6.2 (🔴 MUST, D23): «Поведение тестируем так же, как
# в проде: нужна БД — реальный Postgres». До 26.08.2026 тесты хранилищ ходили на
# SQLite, а прод — на Postgres, и зелёный тест доказывал не то, что работает.
# Вместе с файловыми запасными путями SQLite снесён и отсюда.
#
# Изоляция — СХЕМОЙ на тест, тем же механизмом, которым разведены контуры
# (`FINENGINE_PG_SCHEMA`, `store/engine.py`). Раньше её давал отдельный файл на
# `tmp_path`; схема даёт то же самое и на настоящем движке.
#
# ⚠️ Нет базы — тесты ПРОПУСКАЮТСЯ с внятной причиной, а не падают и не
# подменяются фейком: то же правило 6.2 разрешает `pytest.skip` тестам, которым
# реально нужна инфраструктура. Прогон против живой базы — на стенде:
#
#   bash tests/run_on_stand.sh

class _Адрес(str):
    """Адрес базы, чей repr не показывает пароль.

    pytest на упавшем тесте печатает ЗНАЧЕНИЯ фикстур в заголовке трейсбека —
    голый URL с паролем уезжал в вывод прогона (замечено на стенде 26.08).
    Сам объект остаётся обычной строкой: SQLAlchemy работает с ним как есть.
    """

    def __repr__(self) -> str:  # noqa: D105
        import re
        return repr(re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", str(self)))


_АДРЕС_ТЕСТОВ = "FINENGINE_TEST_DATABASE_URL"
_причина_пропуска: str | None = None
_проверено = False


def _адрес_базы() -> str:
    import os
    return (os.environ.get(_АДРЕС_ТЕСТОВ) or "").strip()


def _база_доступна() -> str | None:
    """Причина, по которой базы нет, или None. Проверяется один раз за прогон."""
    global _причина_пропуска, _проверено
    if _проверено:
        return _причина_пропуска
    _проверено = True
    адрес = _адрес_базы()
    if not адрес:
        # 🔴 СООБЩЕНИЕ НАЗЫВАЕТ ЛОКАЛЬНЫЙ ПУТЬ ПЕРВЫМ (01.09.2026). Раньше оно
        # звало только `run_on_stand.sh` — то есть отправляло каждого нового
        # разработчика просить root на VPS, где стоит и наш прод. Замер показал,
        # что стенд не нужен: против пустого локального Postgres проходят
        # 2809 тестов против 2704 без базы — те же 107, что даёт стенд.
        _причина_пропуска = (
            f"нужен реальный Postgres: {_АДРЕС_ТЕСТОВ} не задан.\n"
            "  Локально:  docker run -d --name fe-pg -e POSTGRES_PASSWORD=pg "
            "-p 5432:5432 postgres:16\n"
            "             export FINENGINE_TEST_DATABASE_URL="
            "postgresql+psycopg://postgres:pg@localhost:5432/postgres\n"
            "  На стенде: bash tests/run_on_stand.sh")
        return _причина_пропуска
    from sqlalchemy import create_engine, text
    try:
        дв = create_engine(адрес, pool_pre_ping=True, future=True, echo=False)
        with дв.begin() as c:
            c.execute(text("select 1"))
        дв.dispose()
    except Exception as e:  # noqa: BLE001 — проба инфраструктуры, а не поток
        _причина_пропуска = f"Postgres по {_АДРЕС_ТЕСТОВ} недоступен: {type(e).__name__}"
    return _причина_пропуска


@pytest.fixture
def бд(request, monkeypatch):
    """Адрес реального Postgres. Своя схема на тест — она же изоляция.

    Возвращает адрес; схема ставится в настройку, и всякий движок, собранный
    через `store/engine.py`, попадает в неё. По выходе схема сносится целиком —
    вместе со всем, что тест успел создать.
    """
    причина = _база_доступна()
    if причина:
        pytest.skip(причина)

    from sqlalchemy import create_engine, text

    from finengine.config import settings

    адрес = _адрес_базы()
    имя = "t_" + "".join(c if c.isalnum() else "_" for c in request.node.name)[:48].lower()
    служебный = create_engine(адрес, pool_pre_ping=True, future=True, echo=False)
    with служебный.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{имя}" CASCADE'))
        c.execute(text(f'CREATE SCHEMA "{имя}"'))
    monkeypatch.setattr(settings, "pg_schema", имя)
    адрес = _Адрес(адрес)
    # Фикстура ДОКАЗЫВАЕТ изоляцию, а не обещает её: без этой проверки тест,
    # у которого схема не применилась, молча пойдёт в `public` — то есть в
    # боевые данные стенда. Один раз так и вышло: сторож увидел 185 документов
    # стенда вместо своего одного и дописал к ним свой.
    from finengine.store.engine import схема
    assert схема() == имя, (
        f"схема контура не применилась: {схема()!r} вместо {имя!r}. "
        "Тест пошёл бы в `public` — то есть в боевые данные стенда")
    yield адрес
    with служебный.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{имя}" CASCADE'))
    служебный.dispose()


@pytest.fixture
def бд_соседняя(request):
    """Вторая схема в той же базе — для тестов, которым нужны ДВА хранилища.

    Переезды сверяют источник и приёмник; на SQLite это были два файла.
    """
    причина = _база_доступна()
    if причина:
        pytest.skip(причина)

    from sqlalchemy import create_engine, text

    адрес = _адрес_базы()
    имя = "s_" + "".join(c if c.isalnum() else "_" for c in request.node.name)[:48].lower()
    служебный = create_engine(адрес, pool_pre_ping=True, future=True, echo=False)
    with служебный.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{имя}" CASCADE'))
        c.execute(text(f'CREATE SCHEMA "{имя}"'))
    yield _Адрес(адрес), имя
    with служебный.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{имя}" CASCADE'))
    служебный.dispose()


@pytest.fixture
def бд_хранилищ(бд, monkeypatch):
    """Три хранилища, у которых больше нет файлового запасного пути.

    Новости, сценарии и реестр документов до 26.08.2026 при пустой настройке
    молча открывали `news.db` / `scenarios.db` / `meta.pkl`. Запасные пути
    снесены — теперь каждый из них требует адрес, и тесты, которым просто нужно
    КАКОЕ-ТО хранилище, заявляют это одной строкой в начале файла:

        pytestmark = pytest.mark.usefixtures("бд_хранилищ")

    Так видно, какие файлы зависят от инфраструктуры, — а не выясняется это
    отказом на середине прогона.
    """
    import finengine.ingestion.document_store as D
    from finengine.config import settings

    for настройка in ("news_database_url", "scenarios_database_url",
                      "documents_database_url"):
        monkeypatch.setattr(settings, настройка, бд)
    D.reset_store()
    yield бд
    D.reset_store()
