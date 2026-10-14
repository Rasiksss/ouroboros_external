"""Контекст извне: без источника и даты строки не существует (#159).

ЧТО ЗАЩИЩАЕТСЯ. Внешний факт («сгорели пункты выдачи», «регулятор поднял
ставку») встаёт в отчёт рядом с числами, которые движок выверил по тексту
отчёта, и выглядит ровно так же авторитетно. Но проверить его пользователю
НЕЧЕМ: это чужой текст из интернета, а не строка баланса. Проверяемым его
делает единственное — имя источника, ссылка и дата. Поэтому здесь стерегут не
«стараемся заполнить поля», а НЕПРОХОДИМОСТЬ: находка без ссылки или без даты
не становится строкой ни в промпте, ни в карточке.

ПУТЬ ТОТ ЖЕ, ЧТО У ПОЛЬЗОВАТЕЛЯ. Тесты ниже дёргают РЕАЛЬНЫЕ функции боевого
пути — `external.collect` → `to_rows`/`from_rows` → `framework.build_prompt` →
`parse.parse_report`. Подменяется ровно одна вещь — сам поход в сеть (граница
Tavily), потому что тест не ходит в интернет. Всё, что после этой границы,
исполняется настоящее.

Стыки, которые в тесте не исполнить (бэкенд → HTTP → запечатанная станция),
проверяются по исходнику — тем же приёмом, что в `test_shared_grounding_live_path.py`.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from finengine.skills import web_search as ws  # noqa: E402
from finengine.skills.analysis import external as ext  # noqa: E402
from finengine.skills.analysis import framework as fw  # noqa: E402
from finengine.skills.analysis.parse import parse_report  # noqa: E402

STATION_TURN = REPO / "src" / "finengine" / "api" / "station_turn.py"


def _code(path: Path) -> str:
    """Исходник без комментариев И БЕЗ ДОКСТРОК.

    Комментарии снимает разбор в дерево, но докстроки переживают `ast.unparse`
    — они обычные строковые выражения. Здесь это принципиально: докстрока
    `station_turn` НАЗЫВАЕТ оборванный стык словами, и сторож, ищущий строку в
    сыром тексте, зеленел бы от объяснительной записки о дефекте, который
    сторожит.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


TOOLS = (REPO / "src" / "finengine" / "llm" / "tools.py").read_text(encoding="utf-8")
STATION = (REPO / "station-skills" / "aifa-analyze" / "scripts" / "analyze.py").read_text(
    encoding="utf-8")
RUN = (REPO / "src" / "finengine" / "skills" / "analysis" / "run.py").read_text(encoding="utf-8")
CARD_TSX = (REPO.parent / "web" / "src" / "components" / "analysis-card.tsx").read_text(encoding="utf-8")


# ── Выдача поисковика: две годные находки и три калеки ──────────────────────
# Калеки взяты не с потолка: ровно так выдача и портится — у агрегаторов часто
# нет даты, у части находок пустой url, а у пресс-релизов пустой заголовок.
GOOD = [
    ws.WebSearchResult(
        title="Компания закрыла часть пунктов выдачи после пожара на складе",
        url="https://www.interfax.ru/business/999001", content="…",
        published_date="2026-03-14"),
    ws.WebSearchResult(
        title="Регулятор сохранил ключевую ставку",
        url="https://cbr.ru/press/pr/999002", content="…",
        published_date="Mon, 20 Apr 2026 09:30:00 GMT"),
]
CRIPPLED = [
    ws.WebSearchResult(title="Без даты — проверить нечем", url="https://rbc.ru/x/1",
                       content="…", published_date=""),
    ws.WebSearchResult(title="Без ссылки — идти некуда", url="",
                       content="…", published_date="2026-05-01"),
    ws.WebSearchResult(title="", url="https://rbc.ru/x/2", content="",
                       published_date="2026-05-02"),
]


def _stub_search(hits):
    """Подмена ровно границы сети: сигнатура как у настоящей `web_search`."""
    async def _fake(query, max_results=5, *, topic="general", days=None):
        _fake.calls.append({"query": query, "topic": topic, "days": days})
        return list(hits)
    _fake.calls = []
    return _fake


@pytest.fixture
def search(monkeypatch):
    """Поиск включён, выдача — под контролем теста."""
    monkeypatch.setattr(ws, "is_enabled", lambda: True)

    def _install(hits):
        fake = _stub_search(hits)
        monkeypatch.setattr(ws, "web_search", fake)
        return fake
    return _install


def _collect(**kw):
    return asyncio.run(ext.collect(**kw))


# ── СТОРОЖ 1: находка без источника/даты не доживает до отчёта ──────────────


def test_crippled_hits_never_become_rows(search):
    """Главный инвариант куска: нет ссылки или даты — нет и строки.

    Проверяется на выдаче, где годных находок ДВЕ, а всего пять.
    """
    search(GOOD + CRIPPLED)
    facts = _collect(company_name="Компания", period="2025", language="ru")
    assert [f.url for f in facts] == [
        "https://cbr.ru/press/pr/999002",     # 2026-04-20 — свежее
        "https://www.interfax.ru/business/999001",
    ]
    for f in facts:
        assert f.text and f.source and f.url and f.date, f"калека прошла фильтр: {f}"


def test_every_surviving_row_carries_source_and_iso_date(search):
    """У выживших дата ПРИВЕДЕНА к ISO, а источник — домен, а не заголовок."""
    search(GOOD)
    rows = ext.to_rows(_collect(company_name="Компания", language="ru"))
    assert {r["source"] for r in rows} == {"interfax.ru", "cbr.ru"}
    assert {r["date"] for r in rows} == {"2026-03-14", "2026-04-20"}
    assert all(set(r) == {"text", "source", "url", "date"} for r in rows)


def test_file_hop_to_the_station_is_filtered_again(search):
    """Между бэкендом и станцией лежит файл — на том берегу дверь та же.

    Файл может приехать из старого прогона или быть подправлен руками; строка
    без источника не должна доехать до промпта ни при каком происхождении.
    """
    smuggled = [
        {"text": "Годная строка", "source": "interfax.ru",
         "url": "https://www.interfax.ru/business/1", "date": "2026-03-14"},
        {"text": "Протащенная мимо сборщика", "source": "выдумка", "url": "", "date": ""},
    ]
    assert [f.text for f in ext.from_rows(smuggled)] == ["Годная строка"]


def test_no_company_name_means_no_search_at_all(search):
    """Без имени компании запроса НЕ делаем: лента «новости» вернёт случайное,
    и это случайное встанет в отчёт как контекст именно этой компании."""
    fake = search(GOOD)
    assert _collect(company_name="  ", language="ru") == []
    assert fake.calls == [], "поиск ушёл без имени компании"


def test_search_asks_for_news_topic_because_only_it_carries_dates(search):
    """Новостной режим — механизм, а не настройка вкуса: дату отдаёт только он,
    а без даты строка всё равно выбывает выше."""
    fake = search(GOOD)
    _collect(company_name="Компания", period="2025", language="ru")
    assert fake.calls, "поиск не состоялся"
    assert all(c["topic"] == "news" for c in fake.calls)
    assert all(c["days"] == ext.NEWS_WINDOW_DAYS for c in fake.calls)
    assert "Компания" in fake.calls[0]["query"] and "2025" in fake.calls[0]["query"]


def test_macro_query_comes_from_the_factory_registry(search):
    """Регуляторы берутся из заводского реестра, а не из литерала в коде."""
    fake = search(GOOD)
    _collect(company_name="Компания", language="ru")
    assert len(fake.calls) == 2, "макро-запрос не ушёл"
    assert "ЦБ РФ" in fake.calls[1]["query"]


# ── СТОРОЖ 2: промпт несёт факты и честную границу ──────────────────────────


def _prompt(facts):
    # Таблица заземления обязательна (#238) — промпта без неё не бывает.
    return fw.build_prompt(
        company_name="Компания", company_type="non_financial", standard="ifrs",
        depth="full", ocr_text="===PAGE 1===\nОтчёт о прибылях и убытках\n",
        metrics_table="SALES_REVENUE | Выручка | 100\n",
        external_facts=facts)


def test_prompt_carries_each_fact_with_its_source_and_date(search):
    search(GOOD)
    facts = _collect(company_name="Компания", language="ru")
    prompt = _prompt(facts)
    assert fw.EXTERNAL_SECTION_TITLE in prompt
    for f in facts:
        assert f.url in prompt and f.date in prompt and f.source in prompt


def test_prompt_forbids_passing_web_off_as_peer_comparison(search):
    """Честная граница из задания: пересказ интернета сравнением не называть."""
    search(GOOD)
    prompt = _prompt(_collect(company_name="Компания", language="ru"))
    assert "НЕ сравнение с конкурентом" in prompt
    assert "не выверены" in prompt  # раздел обязан назвать себя невыверенным


def test_no_facts_means_no_section_header_at_all():
    """Пустой срез не должен превращаться в заголовок без содержимого: модель
    заполнит его по памяти — без ссылки и без даты, то есть домыслом."""
    assert fw.external_block([]) == ""
    assert fw.EXTERNAL_SECTION_TITLE not in _prompt([])


# ── СТОРОЖ 3: карточка берёт строки от сборщика, а не из ответа модели ──────


def test_card_rows_come_from_the_collector_not_from_the_markdown(search):
    """Модель может пересказать факт как угодно — ссылку и дату в карточке
    писал код по ответу источника, и переписать их модель не может."""
    search(GOOD)
    rows = ext.to_rows(_collect(company_name="Компания", language="ru"))
    card = parse_report(
        "# Финансовый анализ — Компания\n\n## Вердикт\n\nВсё ровно.\n",
        company="Компания", external_context=rows)
    assert card["external_context"] == rows
    assert all(r["url"] and r["date"] and r["source"] for r in card["external_context"])


def test_card_external_context_is_empty_without_a_slice():
    card = parse_report("# Финансовый анализ — Компания\n\n## Вердикт\n\nВсё ровно.\n",
                        company="Компания")
    assert card["external_context"] == []


# ── СТОРОЖ 4: живой путь — сбор на бэкенде, доставка файлом, приём в станции ──


def test_the_live_path_collects_the_external_slice():
    """Сбор обязан случиться НА ЖИВОМ ПУТИ, до сборки разбора.

    ⚠️ ПЕРЕСЕЛЁН 20.08.2026 С МЁРТВОЙ ДВЕРИ. Прошлая редакция читала тело
    `tools._h_fe_analyze`, снесённого коммитом aa13cb1, и падала на
    `ValueError: substring not found` — то есть о самом дефекте не сообщала
    ничего. Живой путь анализа сегодня один: `api/station_turn`.

    ПОЧЕМУ ЭТО ДЕФЕКТ, А НЕ НЕДОДЕЛКА. Станция ЗАПЕЧАТАНА, в сеть из неё хода
    нет: собрать внешний срез может только бэкенд и только до передачи работы.
    Станционная половина при этом ЖИВА и ждёт (`analyze.py --external`,
    `_ext.from_rows`), то есть оборван ровно один стык — и оборван молча.
    """
    code = _code(STATION_TURN)
    assert "external_rows=[]" not in code, (
        "внешний срез прибит пустым списком на живом пути — сбор не выполняется "
        "никогда, а пользователю об этом не сообщается")


# ⚠️ УДАЛЁН Срезом 13.5.4: `test_failed_collection_reports_itself_instead_of_
# looking_like_silence`. Держал строку `"внешний контекст недоступен"` в
# исходнике `station_turn.py` — она физически лежала в мёртвом теле
# `_finish_analysis` (код ПОСЛЕ безусловного `return`, недостижимый уже ДО
# этого среза), поэтому проверка была ложно-зелёной: текст в файле есть, путь
# к нему нет. Канонический `analyze.py` вообще не принимает «причину пустоты»
# внешнего среза отдельным каналом (только готовые строки через `--external`)
# — тот, кто решает вызывать станцию с `--external`, не входит в этот срез
# (13.5.4 владеет только слушателем станции и `grounding_bus`, не оркестрацией
# сбора извне). Реальный пробел зафиксирован в `Nedodelano.md`
# («Срез 13.5.4 — внешний контекст: причина пустоты не долетает»), не
# удалён молча.


def test_station_forwards_the_slice_into_the_prompt():
    """Станция обязана прокинуть срез в граф, иначе файл доедет и умрёт.

    ⚠️ Срез 13.5.4: доставка сменила форму. Раньше станция передавала срез
    ПРЯМО в скилл-функцию (`skills.analysis.run`, `external_facts=`
    аргументом). Канонический `analyze.py` регистрирует его как EXECUTOR
    РЕЕСТРА (`registry_options["external_executor"]`), который вызовет узел
    `explain.external` внутри графа, когда до него дойдёт очередь, — тот же
    `ext.from_rows`, но подключённый на уровне registry, не прямого вызова
    промпт-билдера. `skills/analysis/run.py` больше не на боевом пути станции
    (analyze.py его не импортирует вовсе) — не проверяем его здесь.
    """
    assert "ext.from_rows(rows)" in STATION
    assert 'registry_options={"external_executor": _external_executor(external_rows)}' in STATION, (
        "станция перестала регистрировать executor внешнего контекста — "
        "explain.external останется без среза"
    )


def test_frontend_hides_rows_without_a_source():
    """Второй рубеж на фронте: карточка из старого диалога тоже под правилом."""
    assert "function isCitable" in CARD_TSX
    guard = CARD_TSX[CARD_TSX.index("function isCitable"):]
    guard = guard[: guard.index("\n}")]
    for field in ("text", "source", "url", "date"):
        assert f"x.{field}?.trim()" in guard, f"поле {field} не проверяется"
    assert "(card.external_context ?? []).filter(isCitable)" in CARD_TSX


# ── Согласие пользователя, а не наличие ключа ──────────────────────────────


def test_web_search_toggle_off_means_nothing_leaves_the_stand(search, monkeypatch):
    """ТУМБЛЕР «Поиск в интернете» — согласие, а не стиль.

    Он по умолчанию ВЫКЛЮЧЕН и настоящий: бэк вырезает поисковые инструменты из
    набора модели. Сбор внешнего среза — второй, НОВЫЙ выход наружу, и он
    отправляет туда ИМЯ КОМПАНИИ из отчёта пользователя. Пока сбор смотрел
    только на наличие ключа Tavily, человек с надписью «выключен» при каждом
    анализе сообщал наружу, чей отчёт он читает.

    Ходим настоящим `set_web_search` (его зовёт ручка чата), а не подменяем
    функцию проверки: проверять надо ту дорогу, по которой идёт тумблер.
    """
    from finengine.llm.openclaw import set_web_search

    called = search([ws.WebSearchResult(
        title="Новость про компанию", url="https://www.interfax.ru/business/1",
        content="", published_date="2026-05-01")])
    set_web_search(False)
    try:
        assert _collect(company_name="Компания", period="2025", language="ru") == []
        assert not called.calls, "имя компании ушло наружу при выключенном тумблере"
    finally:
        set_web_search(True)


def test_web_search_toggle_on_still_collects(search):
    """Противовес: тумблер включён — сбор работает, иначе мы «починили» его в ноль."""
    from finengine.llm.openclaw import set_web_search

    search([ws.WebSearchResult(
        title="Новость про компанию", url="https://www.interfax.ru/business/1",
        content="", published_date="2026-05-01")])
    set_web_search(True)
    assert _collect(company_name="Компания", period="2025", language="ru")


# ── Один раздел, а не два ──────────────────────────────────────────────────


def test_model_retelling_of_the_external_list_does_not_become_a_second_section():
    """Карточка рисует внешние факты САМА — списком, где у каждой строки
    источник, дата и ссылка. Пересказ этого списка моделью вставал рядом вторым
    разделом с тем же заголовком, но БЕЗ ссылок, и получал авторитет соседних
    выверенных чисел даром. Промпт такого раздела больше не просит, но просьба —
    не гарантия."""
    md = (f"# Финансовый анализ — Х\n\n## Вердикт\nтекст\n\n"
          f"## {fw.EXTERNAL_SECTION_TITLE}\nВыручка упала из-за пожара на складе.\n")
    card = parse_report(md, company="Х")

    # Разбор на разделы снят 11.08 — отчёт едет одним полем. Проверяем на нём:
    # блок от модели вырезан, остальной текст на месте.
    report = card["report_md"]
    assert fw.EXTERNAL_SECTION_TITLE not in report, (
        f"раздел от модели встал в карточку рядом с выверенными:\n{report}")
    assert "пожаре" not in report and "пожаре на складе" not in report
    assert "Вердикт" in report and "текст" in report, "остальной отчёт потерян"


def test_the_prompt_no_longer_asks_for_that_section():
    """Второй рубеж в разборе не должен подменять первый: промпт обязан сам
    просить НЕ писать раздел, иначе модель каждый раз пишет впустую."""
    facts = ext.from_rows([{"text": "Новость", "source": "e.ru",
                            "url": "https://e.ru/a", "date": "2026-05-01"}])
    block = fw.external_block(facts)
    assert "НЕ пиши" in block and fw.EXTERNAL_SECTION_TITLE in block
