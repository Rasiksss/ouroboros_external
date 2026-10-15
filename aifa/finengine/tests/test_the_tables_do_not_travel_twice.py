"""Тела таблиц не едут модели дважды — по крайней мере, когда это разрешено.

ЗАМЕР (21.08.2026, три настоящих отчёта из `data/documents`). Первая стадия
получает ВЕСЬ текст отчёта И каталог таблиц, а тела таблиц в каталоге —
буквальный повтор того, что в тексте уже есть:

    ab-probe   368 238 знаков текста | каталог 269 141 (73 % текста)
    3f7092b7   206 114               | каталог  95 941 (47 %)
    megafon     76 928               | каталог  54 538 (71 %)

Промпт больше нужного в полтора-два раза, а сжатие — 216 секунд из 318.

ЧТО В КАТАЛОГЕ НУЖНО, А ЧТО ДУБЛИРУЕТСЯ. Нужна АДРЕСАЦИЯ: модель называет
улики идентификатором таблицы, и взять его неоткуда — в тексте отчёта
идентификаторов нет. Поэтому `table_id`, страница, заголовок и соседний текст
остаются в любом режиме; сокращается только ТЕЛО, и в тексте оно никуда не
девается.

⚠️ ВЫКЛЮЧЕНО ПО УМОЛЧАНИЮ. Промпт сжатия — сердце извлечения, а
`docs/rules/architecture.md` называет регрессию качества блокером мержа.
Ускорение, не проверенное замером качества, — ставка, а не улучшение. Поэтому
здесь два теста-близнеца: один держит УМОЛЧАНИЕ (ничего не изменилось), другой
проверяет РЕЖИМ. Включать после A/B.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · умолчание сменить на `short` → падает `test_by_default_nothing_changes`;
  · в коротком режиме резать и `context_before`/`context_after` → падает
    `test_the_short_mode_keeps_the_addressing`;
  · `_SHORT_TABLE_LINES = 0` (тело вырезано целиком) → падает
    `test_the_short_mode_still_lets_a_table_be_recognised`.
"""
from __future__ import annotations


import pytest

_TABLE = "\n".join([
    "| Показатель | 2025 | 2024 |",
    "| --- | --- | --- |",
] + [f"| Строка {i} | {i * 10} | {i * 9} |" for i in range(1, 40)])


def _catalog(mode: str, monkeypatch) -> str:
    """Каталог того же набора таблиц в заданном режиме.

    🔴 БЕЗ importlib.reload(finengine.config) — ЗДЕСЬ ОН СТОЯТЬ НЕ ДОЛЖЕН.
    reload исполняет config.py повторно и перепривязывает `settings` к НОВОМУ
    объекту; все модули, взявшие `from finengine.config import settings` на
    импорте, до конца прогона держат осиротевший старый — правка настройки в
    любом следующем тесте видна одному коду и не видна другому. Ровно из-за
    этого reload сторож контуров однажды ушёл в схему public и увидел 185
    боевых документов стенда вместо своего одного (следствие 27.08.2026,
    доказано стеком исполнений config.py). Потребитель читает настройку лениво
    в момент вызова (`_table_catalog`), так что хватает monkeypatch.
    """
    from finengine.config import settings

    monkeypatch.setattr(settings, "compression_table_bodies", mode)
    import finengine.skills.two_stage_extractor as ts

    tables = ts._extract_tables_from_text(
        f"Консолидированный отчёт\n\nПримечание 5. Выручка\n\n{_TABLE}\n\nдалее по тексту")
    assert tables, "разбор не нашёл ни одной таблицы — мерить нечего"
    return ts._table_catalog(tables)


def test_by_default_nothing_changes(monkeypatch):
    """Умолчание — полное тело. Ускорение без замера качества не включают.

    ⚠️ ПЕРВАЯ РЕДАКЦИЯ ЭТОГО ТЕСТА БЫЛА ПУСТОЙ, и мутация это показала: он
    выставлял `full` переменной окружения и тем самым проверял НЕ УМОЛЧАНИЕ, а
    собственную подстановку. Смена умолчания на `short` проходила зелёной —
    то есть тихое включение непроверенного ускорения он бы пропустил.
    Теперь переменная СНИМАЕТСЯ, и спрашивается сам `Settings`.
    """
    monkeypatch.delenv("FINENGINE_COMPRESSION_TABLE_BODIES", raising=False)
    import finengine.config as c

    # СВЕЖИЙ экземпляр, а не reload модуля: reload перепривязал бы глобальный
    # `settings` и осиротил все связанные на импорте ссылки (см. _catalog).
    # Settings() точно так же перечитывает env и .env.
    свежий = c.Settings()
    assert свежий.compression_table_bodies == "full", (
        f"умолчание стало {свежий.compression_table_bodies!r} — сердце "
        "извлечения переключено без замера качества, а регрессия качества по "
        "docs/rules/architecture.md блокер мержа")


def test_the_short_mode_shortens_the_body(monkeypatch):
    short = _catalog("short", monkeypatch)
    full = _catalog("full", monkeypatch)
    assert len(short) < len(full) * 0.6, (
        f"короткий режим дал {len(short)} против {len(full)} — экономии почти нет")
    assert "Строка 39" not in short, "хвост таблицы всё ещё едет вторым разом"


def test_the_short_mode_keeps_the_addressing(monkeypatch):
    """Адресация — то, ради чего каталог существует, и она НЕ режется.

    Модель называет улики идентификатором таблицы; в тексте отчёта их нет.
    Срежь заголовок или соседний текст — и таблицу станет нечем опознать.
    """
    short = _catalog("short", monkeypatch)
    for нужное in ("table_id=", "page=", "title:", "context_before:", "context_after:"):
        assert нужное in short, f"короткий режим потерял адресацию: {нужное}"


def test_the_short_mode_still_lets_a_table_be_recognised(monkeypatch):
    """Шапка остаётся: по ней модель сопоставляет запись каталога с текстом."""
    short = _catalog("short", monkeypatch)
    assert "| Показатель | 2025 | 2024 |" in short, (
        "шапка таблицы вырезана — запись каталога стало нечем сопоставить с "
        "текстом отчёта, и адресация превратилась в набор безымянных номеров")
    assert "ещё" in short and "строк" in short, (
        "не сказано, что таблица урезана и лежит целиком в тексте — модель "
        "решит, что таблица такая и есть")


@pytest.mark.parametrize("mode", ["full", "short"])
def test_every_table_keeps_its_own_id(mode, monkeypatch):
    """Идентификаторы не схлопываются: улика без адреса непроверяема."""
    cat = _catalog(mode, monkeypatch)
    ids = [line for line in cat.splitlines() if line.startswith("## table_id=")]
    assert len(ids) == len(set(ids)), f"[{mode}] идентификаторы таблиц повторяются: {ids}"
