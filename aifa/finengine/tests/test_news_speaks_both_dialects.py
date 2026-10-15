"""Новостной конвейер: тот же файл читается как есть, SQLite-только конструкций нет.

🔴 ЗАЧЕМ (решение владельца 25.08.2026). `skills/news/db.py` был написан на
голом `sqlite3` с `PRAGMA`, `sqlite_master`, `executescript`, `INSERT OR IGNORE`
и `PRAGMA table_info`. Всё это есть ТОЛЬКО у SQLite: модуль не запускался на
Postgres в принципе, то есть `docs/rules/architecture.md` 2.1 (🔴 MUST)
«Postgres — единственная БД… признак регрессии: файловые БД» было невыполнимо
технически, а не просто нарушено.

САМОЕ ВАЖНОЕ СВОЙСТВО — СОВМЕСТИМОСТЬ. В базе 235 тысяч строк сырья и 116
прогонов; переписать слой доступа и не суметь их прочитать значит потерять
собранный корпус. Поэтому первые тесты — не про Postgres, а про то, что
накопленный файл читается новым кодом без переноса и отвечает ТЕМ ЖЕ.
"""
from __future__ import annotations

import sqlite3

import pytest

import finengine.skills.news.db as news
from finengine.config import settings
from finengine.store.migrate_news import migrate

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



@pytest.fixture
def файл(бд_хранилищ, tmp_path, monkeypatch):
    """Накопленная база прежнего кода — и она же, ПЕРЕВЕЗЁННАЯ в Postgres.

    ⚠️ 26.08.2026 фикстура изменила смысл. Раньше файл был БОЕВЫМ хранилищем:
    пустая настройка открывала `news.db`, и модуль читал его напрямую. Запасной
    путь снесён — модуль знает только базу. Файл остался тем, чем он и является
    сегодня: НАСЛЕДИЕМ, из которого данные приезжают переездом. Поэтому фикстура
    строит его точно так же (миграция v1 + два ALTER-а), а потом прогоняет
    переезд — и тесты ниже проверяют то же поведение на настоящем движке.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    conn = sqlite3.connect(str(tmp_path / "news.db"), isolation_level=None)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            source_type TEXT NOT NULL, region TEXT NOT NULL, url TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 0.5, active INTEGER NOT NULL DEFAULT 1,
            last_fetched REAL);
        CREATE TABLE raw_items (
            raw_hash TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT NOT NULL,
            snippet TEXT, url TEXT NOT NULL, published_at REAL NOT NULL,
            fetched_at REAL NOT NULL, matched_json TEXT);
        CREATE TABLE digest_runs (
            run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
            profile TEXT NOT NULL, items_raw INTEGER, items_kept INTEGER,
            llm_in INTEGER, llm_out INTEGER, cost_usd REAL, error TEXT);
        CREATE TABLE digest_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            raw_hash TEXT NOT NULL, is_relevant INTEGER NOT NULL, category TEXT,
            region TEXT, importance REAL, final_score REAL,
            in_long_list INTEGER NOT NULL DEFAULT 0, watchlist_match TEXT,
            one_sentence TEXT, why_important TEXT);
        INSERT INTO schema_version(version) VALUES (1);
        INSERT INTO sources VALUES ('cbr','Банк России','regulator','ru','http://cbr',0.9,1,NULL);
        INSERT INTO raw_items VALUES ('h1','cbr','Ставка снижена','подробности','http://a',1000.0,1000.0,NULL);
        INSERT INTO raw_items VALUES ('h2','cbr','Про банки','ещё','http://b',900.0,900.0,NULL);
        INSERT INTO digest_runs VALUES ('r1',500.0,600.0,'ir_market_news',10,2,1,1,0.01,NULL);
        INSERT INTO digest_items VALUES (1,'r1','h1',1,'rates','ru',0.9,0.9,1,NULL,'фраза модели','важно');
    """)
    conn.close()
    news._engines.clear()
    migrate(f"sqlite:///{tmp_path / 'news.db'}", бд_хранилищ)
    yield tmp_path
    news._engines.clear()


def test_the_old_file_arrives_whole_through_the_migration(файл):
    """🔴 ГЛАВНОЕ: накопленная база доезжает ЦЕЛИКОМ и читается без init_schema.

    ⚠️ Файл здесь заведомо СТАРЫЙ: в `digest_runs` нет колонок `user_id` и
    `diag` — их дописывали ALTER-ом на живой базе. Переезд обязан справиться с
    записью, не знающей про поля, заведённые после неё, а чтение после него —
    работать до всякого `init_schema`.
    """
    пункты = news.digest_items_for_run("r1")
    assert len(пункты) == 1
    assert пункты[0]["title"] == "Ставка снижена"
    assert пункты[0]["one_sentence"] == "фраза модели"
    assert пункты[0]["source_name"] == "Банк России"
    assert пункты[0]["source_id"] == "cbr", (
        "source_id сырья пропал — блок регуляторов уйдёт в архивный fallback")


def test_init_schema_adds_the_columns_that_alter_used_to_add(файл):
    """`user_id` и `diag` дописываются один раз на старте, а не на каждом запросе."""
    news.init_schema()
    from sqlalchemy import inspect as sa_inspect

    колонки = {c["name"] for c in sa_inspect(news.engine()).get_columns("digest_runs")}
    assert {"user_id", "diag"} <= колонки
    news.init_schema()  # идемпотентность
    assert len(news.digest_items_for_run("r1")) == 1


def test_owner_comparison_survives_null(файл):
    """⚠️ `NULL = NULL` в SQL ложно — прогоны планировщика искать надо через IS.

    Сравни через `==`, и выборка выпусков планировщика (`user_id IS NULL`) молча
    опустеет: пользователь откроет «Дайджест» и не увидит ничего.
    """
    news.init_schema()
    прогон = news.latest_digest_run("ir_market_news", user_id=None)
    assert прогон is not None and прогон["run_id"] == "r1"
    assert news.latest_digest_run("ir_market_news", user_id=42) is None


def test_insert_ignore_reports_whether_it_was_new(файл):
    """На возврате стоит подсчёт «сколько новых новостей собрали»."""
    from datetime import datetime, timezone

    from finengine.skills.news.models import NewsItem

    def _вещь(хеш: str, заголовок: str, когда: float) -> NewsItem:
        return NewsItem(
            source="Банк России", source_id="cbr", source_type="regulator",
            region="ru", source_weight=0.9, title=заголовок, snippet="",
            url=f"http://{хеш}", raw_hash=хеш,
            published_at=datetime.fromtimestamp(когда, timezone.utc))

    assert news.upsert_raw_item(_вещь("h1", "повтор", 1000.0)) is False, (
        "дубль засчитан как новый — счётчик «сколько собрали» соврёт")
    assert news.upsert_raw_item(_вещь("h9", "новое", 1100.0)) is True


def test_upsert_source_updates_instead_of_duplicating(файл):
    news.upsert_source("cbr", "Банк России (новое имя)", "regulator", "ru",
                       "http://cbr", 0.7, True)
    строки = news.recent_items_from_sources(["cbr"], max_age_days=36500, limit=10)
    assert строки and строки[0]["source_name"] == "Банк России (новое имя)"
    assert строки[0]["weight"] == 0.7


def test_search_looks_in_all_four_text_fields(файл):
    """Пользователь спрашивает словами из заголовка, ответ чаще в изложении."""
    news.init_schema()
    for слово in ("Ставка", "подробности", "фраза", "важно"):
        assert news.search_digest_items(слово, days=36500, limit=5), (
            f"поиск не нашёл по слову {слово!r} — потеряно одно из четырёх полей")


def test_dedup_is_per_owner(файл):
    """Дедуп по владельцу: чужая история не должна подавлять мою."""
    news.init_schema()
    assert news.recent_long_list_hashes(days=36500, user_id=None) == set(), (
        "планировщику дедуп не нужен — подавлять ему нечего")
    assert news.recent_long_list_hashes(days=36500, user_id=1) == set(), (
        "прогон принадлежит планировщику (user_id NULL), а не пользователю 1")


def test_no_sqlite_only_constructs_remain():
    """`PRAGMA`, `sqlite_master`, `executescript`, `INSERT OR IGNORE`.

    Способ проверки — AST с отбросом ДОКСТРИНГОВ: они перечисляют эти
    конструкции как убранные, и наивный поиск по тексту краснел бы на них.
    Остальные строковые литералы смотрим — SQL живёт именно в них.
    """
    import ast
    from pathlib import Path

    путь = (Path(__file__).resolve().parents[1] / "src" / "finengine"
            / "skills" / "news" / "db.py")
    дерево = ast.parse(путь.read_text(encoding="utf-8"))
    докстринги = set()
    for узел in ast.walk(дерево):
        тело = getattr(узел, "body", None)
        if isinstance(узел, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
           and тело and isinstance(тело[0], ast.Expr) \
           and isinstance(тело[0].value, ast.Constant) and isinstance(тело[0].value.value, str):
            докстринги.add(id(тело[0].value))
    литералы = " ".join(n.value for n in ast.walk(дерево)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and id(n) not in докстринги)
    for конструкция in ("PRAGMA", "sqlite_master", "executescript", "INSERT OR IGNORE"):
        assert конструкция not in литералы, (
            f"в КОДЕ осталась SQLite-только конструкция {конструкция!r}")
    импорты = {a.name for n in ast.walk(дерево) if isinstance(n, ast.Import) for a in n.names}
    assert "sqlite3" not in импорты


def test_migration_is_additive_and_ordered(файл, tmp_path):
    """Повтор не задваивает; порядок таблиц не оставляет битых ссылок."""
    цель = f"sqlite:///{tmp_path / 'target.db'}"
    о = migrate(f"sqlite:///{файл / 'news.db'}", цель)
    assert о["raw_items_перенесено"] == 2 and о["digest_items_перенесено"] == 1

    о2 = migrate(f"sqlite:///{файл / 'news.db'}", цель)
    assert о2["raw_items_перенесено"] == 0 and о2["digest_items_перенесено"] == 0

    # ссылки целы: пункт находит своё сырьё и свой источник
    import sqlalchemy as sa
    e = sa.create_engine(цель, future=True)
    with e.begin() as c:
        n = c.execute(sa.text(
            "select count(*) from digest_items d "
            "join raw_items r on r.raw_hash=d.raw_hash "
            "join sources s on s.source_id=r.source_id")).scalar()
    assert n == 1, "после переезда пункт потерял сырьё или источник"


def test_a_missing_address_is_a_loud_refusal_not_a_quiet_file(monkeypatch):
    """🔴 ФАЙЛОВОГО УМОЛЧАНИЯ БОЛЬШЕ НЕТ (26.08.2026).

    Здесь проверялось обратное: пустая настройка = тот же файл, что и раньше.
    Пока так было, ненастроенный контур работал «как будто получилось» — писал
    в базу новостей, которую никто не читает. Мутация: вернуть открытие файла
    вместо отказа — тест краснеет.
    """
    from finengine.config import settings as s

    monkeypatch.setattr(s, "news_database_url", "")
    with pytest.raises(RuntimeError, match="NEWS_DATABASE_URL"):
        news.engine()
