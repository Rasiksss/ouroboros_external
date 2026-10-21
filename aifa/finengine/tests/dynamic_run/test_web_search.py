"""Часть 19 доводки: поиск во внешних источниках для цикла.

Исходный случай (dev, 08.10.2026, отчёт ЛУКОЙЛа за 1П 2026): «найди в интернете, что происходило с ключевой
ставкой ЦБ и курсом рубля в 2026 году, и объясни, как это связано с цифрами в отчёте». У цикла не было
поиска; тумблер «Поиск в интернете» был выключен, а планировщик видел только «вернули пусто».

Запрос поиска — поле ``concept`` шага (решение владельца 08.10.2026: индекс готовых результатов Case
различает результаты по концепту, а не по ``extra``), режим — ``extra.topic``.

Подставные — только модели, поисковик и сеть загрузки; ``Case`` — SQLite, граф и цикл настоящие.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from finengine.config import settings
from finengine.contracts import DynamicArtifact
from finengine.llm.openclaw import set_web_search
from finengine.skills import web_search as ws
from finengine.skills.analysis.external import WEB_SEARCH_OFF_REASON
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.atoms.web_search import WEB_SEARCH_TYPE
from finengine.skills.spec.atom_catalog import ATOM_DESCRIPTIONS, catalog_text
from finengine.skills.spec.dynamic_run import RunLimits
from finengine.skills.spec.graph_executor import RunInputs
from tests.integration.conftest import _make_fake_locate_and_read_value_executors
from tests.integration.ingress_support import (  # noqa: F401
    gate_env,
    patched_build_registry,
    set_intent,
)

from subtask_support import SubtaskCodes, patch_subtask
from test_chat_outcome_contract import SequenceModel, _arm_dynamic
from test_download_atom import _atom as _download_atom
from test_download_atom import _csv_ok, _serve
from test_download_atom import _run as _run_download
from test_dynamic_run import _give_up as _give_up_reply
from test_dynamic_run import _move as _move_reply
from test_dynamic_run import _run as _cycle
from test_ephemeral_in_graph import _stored
from test_goal_by_reference import MESSAGE as BRENT_MESSAGE
from test_goal_by_reference import BRENT_ATOM, STATS_ATOM, _arm, _finalize_stats, _ids, _move, _text, _turn

ROOT = Path(__file__).resolve().parents[2]
MESSAGE = (
    "найди в интернете, что происходило с ключевой ставкой ЦБ и курсом рубля в 2026 году, "
    "и объясни, как это связано с цифрами в отчёте"
)
RATE_QUERY = "ключевая ставка ЦБ РФ решения 2026"
FX_QUERY = "курс рубля к доллару 2026 динамика"
PAGE_URL = "https://data.example.org/news/key-rate-2026-02-13.html"
PAGE_HTML = (
    "<html><head><title>Банк России</title><script>var ставка = 99;</script>"
    "<style>p { color: red }</style></head>"
    "<body><p>Ключевая <b>ставка</b> 16 % годовых с 13.02.2026</p></body></html>"
)


@dataclass
class Search:
    """Подставной поисковик: записывает аргументы каждого вызова."""
    hits: list[ws.WebSearchResult]
    calls: list[dict] = field(default_factory=list)

    async def __call__(self, query: str, max_results: int = 5, *, topic: str = "general", days=None):
        self.calls.append({"query": query, "topic": topic, "max_results": max_results})
        return list(self.hits)


def _hits(count: int = 2, content: str = "Совет директоров Банка России принял решение.") -> list[ws.WebSearchResult]:
    first = ws.WebSearchResult(title="Ключевая ставка: решение от 13 февраля", url=PAGE_URL,
                               content=content, published_date="2026-02-13")
    rest = [
        ws.WebSearchResult(
            title=f"Обзор денежно-кредитной политики, выпуск {index}",
            url=f"https://data.example.org/reviews/2026/monetary-policy-review-number-{index}?section=rates",
            content=content, published_date=f"2026-03-{index:02d}",
        )
        for index in range(1, count)
    ]
    return [first, *rest]


@pytest.fixture
def search(monkeypatch) -> Search:
    fake = Search(_hits())
    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    monkeypatch.setattr(ws, "web_search", fake)
    return fake


@pytest.fixture
def web_off():
    """Тумблер «Поиск в интернете» выключен — тем же `set_web_search`, который зовёт ручка чата."""
    set_web_search(False)
    yield
    set_web_search(True)


def _search_step(query: str, topic: str = "news") -> dict:
    return {"atom": "web.search", "concept": query, "period_ref": None, "extra": {"topic": topic}}


def _artifacts_block(state: str) -> str:
    return state.split("## Полученные артефакты\n", 1)[1].split("\n\n## Журнал ходов", 1)[0]


# --- К19.1: планировщик → web.search ----------------------------------------------------------------


def test_K19_1_the_query_reaches_the_search_engine_verbatim_and_the_result_is_a_dynamic_artifact(base, search) -> None:
    dynamic, _ = _cycle(base, [_move_reply(steps=(_search_step(RATE_QUERY),)), _give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    assert search.calls == [{"query": RATE_QUERY, "topic": "news", "max_results": settings.web_search_max_results}]
    (artifact,) = _stored(base, WEB_SEARCH_TYPE)
    assert isinstance(artifact, DynamicArtifact) and artifact.origin == ["external"]
    assert artifact.payload["query"] == RATE_QUERY and artifact.payload["topic"] == "news"
    assert [hit["url"] for hit in artifact.payload["hits"]] == [hit.url for hit in search.hits]
    assert artifact.payload["hits"][0]["published_date"] == "2026-02-13"
    assert artifact.producer.startswith("web.search@")


def test_K19_1_two_queries_in_one_move_are_two_nodes_and_a_repeat_is_not_a_new_call(base, search) -> None:
    both = _move_reply(steps=(_search_step(RATE_QUERY), _search_step(FX_QUERY)))
    dynamic, planner = _cycle(
        base, [both, _move_reply(steps=(_search_step(RATE_QUERY),)), _give_up_reply()],
        limits=RunLimits(idle_moves=5),
    )

    dynamic.run(deterministic_error="нет пути")

    assert [call["query"] for call in search.calls] == [RATE_QUERY, FX_QUERY], "повтор запроса поисковик не зовёт"
    assert {item.payload["query"] for item in _stored(base, WEB_SEARCH_TYPE)} == {RATE_QUERY, FX_QUERY}
    assert "уже было" in planner.prompts[2]


def test_the_search_mode_is_part_of_the_step_and_general_is_the_default(base, search) -> None:
    step = {"atom": "web.search", "concept": RATE_QUERY, "period_ref": None, "extra": {}}
    dynamic, _ = _cycle(base, [_move_reply(steps=(step,)), _give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    assert search.calls[0]["topic"] == "general"


@pytest.mark.parametrize("step, expected", [
    ({"atom": "web.search", "concept": None, "extra": {"topic": "news"}}, "concept"),
    ({"atom": "web.search", "concept": "   ", "extra": {}}, "concept"),
    ({"atom": "web.search", "concept": RATE_QUERY, "extra": {"topic": "images"}}, "general"),
])
def test_a_search_step_without_a_query_or_with_an_unknown_mode_is_rejected_before_the_search(
    base, search, step, expected,
) -> None:
    dynamic, planner = _cycle(base, [_move_reply(steps=(step,)), _give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    assert search.calls == []
    assert "шаги отклонены" in planner.prompts[1] and expected in planner.prompts[1].split("## Итог прошлого хода")[1]


# --- К19.2: web.search → Case → состояние → планировщик -----------------------------------------------


def test_K19_2_the_state_shows_every_hit_with_its_whole_address_within_the_named_limit(base, search) -> None:
    search.hits = _hits(10, content="Подробности решения. " * 80)
    dynamic, planner = _cycle(base, [_move_reply(steps=(_search_step(RATE_QUERY),)), _give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    block = _artifacts_block(planner.prompts[1])
    row = next(line for line in block.splitlines() if f"| {WEB_SEARCH_TYPE} |" in line)
    assert RATE_QUERY in row, "запрос виден в строке артефакта"
    for hit in search.hits:
        assert hit.url in block, f"адрес обрезан: {hit.url}"
        assert hit.title in block
    assert "2026-02-13" in block
    shown = block.split(f"| {WEB_SEARCH_TYPE} | {RATE_QUERY} | — | ", 1)[1]
    assert len(shown) <= settings.web_search_state_chars, len(shown)
    assert "Подробности решения." in shown, "фрагменты ужаты, а не выброшены"


def test_other_dynamic_artifacts_are_shown_as_before(base, monkeypatch) -> None:
    _serve(monkeypatch, _csv_ok)
    download = _download_atom()
    move = _move_reply(atoms=(download.request,), steps=({"atom": download.request.name},))
    dynamic, planner = _cycle(base, [move, _give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    row = next(line for line in _artifacts_block(planner.prompts[1]).splitlines() if "| dyn:brent_csv |" in line)
    assert row.split("|")[4].strip().startswith('{"url": "https://data.example.org/brent.csv"')


# --- К19.3: находка → атом-загрузка → атом с кодом ----------------------------------------------------


def _html_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path != "/news/key-rate-2026-02-13.html":
        return httpx.Response(404)
    return httpx.Response(200, content=PAGE_HTML.encode(), headers={"content-type": "text/html; charset=utf-8"})


def test_K19_3_an_html_page_is_downloaded_as_text_without_markup_scripts_and_styles(monkeypatch) -> None:
    _serve(monkeypatch, _html_ok)

    (artifact,) = _run_download(_download_atom("eph.rate_page", PAGE_URL, "dyn:rate_page"))

    text = artifact.payload["text"]
    assert "Ключевая ставка 16 % годовых с 13.02.2026" in text
    assert "<" not in text and "var ставка" not in text and "color: red" not in text
    assert artifact.payload["content_type"] == "text/html" and artifact.origin == ["external"]


def test_the_text_of_a_long_page_is_cut_to_the_limit(monkeypatch) -> None:
    body = "<html><body>" + "<p>абзац текста страницы</p>" * 20_000 + "</body></html>"
    _serve(monkeypatch, lambda request: httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/html"}))

    (artifact,) = _run_download(_download_atom("eph.rate_page", PAGE_URL, "dyn:rate_page"))

    assert len(artifact.payload["text"]) == settings.download_page_text_chars


def test_json_and_csv_are_downloaded_byte_for_byte_as_before(monkeypatch) -> None:
    body = '{"rows": [{"date": "2026-01-02", "note": "<b>не разметка</b>"}]}\n'
    _serve(monkeypatch, lambda request: httpx.Response(
        200, content=body.encode(), headers={"content-type": "application/json"}))

    (artifact,) = _run_download(_download_atom())

    assert artifact.payload["text"] == body


def test_the_free_chat_tool_and_the_download_atom_strip_markup_with_the_same_function() -> None:
    import inspect

    from finengine.llm import tools
    from finengine.skills.document_acquisition import page_text
    from finengine.skills.spec import ephemeral

    fetch_url = inspect.getsource(tools._h_fetch_url)
    assert "html_to_text(html)" in fetch_url and "re.sub(" not in fetch_url, "свободный чат чистит разметку своей копией"
    assert ephemeral.html_to_text is page_text.html_to_text
    assert page_text.html_to_text(PAGE_HTML) == "Банк России Ключевая ставка 16 % годовых с 13.02.2026"


# --- К19.4: тумблер → отказ атома → итог хода → планировщик -------------------------------------------


def test_K19_4_with_the_toggle_off_the_search_node_fails_with_the_reason_and_nothing_is_searched(
    base, search, web_off,
) -> None:
    dynamic, planner = _cycle(base, [_move_reply(steps=(_search_step(RATE_QUERY),)), _give_up_reply()])

    result = dynamic.run(deterministic_error="нет пути")

    outcome = result.journal[0].outcome
    assert search.calls == [], "запрос ушёл наружу при выключенном тумблере"
    assert WEB_SEARCH_OFF_REASON in outcome and "вернули пусто" not in outcome
    assert outcome.startswith("ход завершён с ошибками")
    assert WEB_SEARCH_OFF_REASON in planner.prompts[1]
    assert _stored(base, WEB_SEARCH_TYPE) == []


def test_K19_4_the_download_atom_checks_the_same_toggle_and_gives_the_same_reason(monkeypatch, web_off) -> None:
    seen = _serve(monkeypatch, _csv_ok)

    with pytest.raises(Exception, match=re.escape(WEB_SEARCH_OFF_REASON)):
        _run_download(_download_atom())

    assert seen == [], "обход выключенного поиска через атом-загрузку"


def test_K19_4_an_empty_explain_external_under_the_toggle_gets_the_reason_not_just_empty(
    base, search, web_off, severstal_text,
) -> None:
    step = {"atom": "explain.external", "concept": None, "period_ref": "2024", "extra": {}}
    passport = build_run_passport(
        severstal_text, goal_ir_id="goal-1", classification=classification_from_cell("ifrs_nf_ru"))
    dynamic, planner = _cycle(base, [_move_reply(steps=(step,)), _give_up_reply()], run_passport=passport)
    dynamic.inputs = RunInputs(document_text=severstal_text, role="other")

    dynamic.run(deterministic_error="нет пути")

    feedback = planner.prompts[1].split("## Итог прошлого хода")[1]
    assert search.calls == []
    assert "explain.external вернули пусто" in feedback, feedback
    assert f"explain.external: {WEB_SEARCH_OFF_REASON}" in feedback


def test_the_reason_tells_the_user_what_to_switch_on() -> None:
    assert "выключен пользователем" in WEB_SEARCH_OFF_REASON and "«Поиск в интернете»" in WEB_SEARCH_OFF_REASON


# --- К19.5: каталог ↔ поведение атомов ---------------------------------------------------------------


def test_K19_5_the_catalog_says_what_each_external_atom_really_does(base) -> None:
    text = catalog_text(base.registry)
    search_line = next(line for line in text.splitlines() if line.startswith("- web.search"))
    external = ATOM_DESCRIPTIONS["explain.external"]

    assert "concept" in search_line and "topic" in search_line
    assert "general" in search_line and "news" in search_line and "дат" in search_line
    assert "чисел отчётности" in search_line and WEB_SEARCH_TYPE in search_line
    assert "web.search" in external and "компани" in external
    assert "тему задать нельзя" in external, "explain.external не принимает тему — каталог говорит это прямо"


# --- К19.6: отказ проверки шагов → планировщик --------------------------------------------------------


def test_K19_6_an_undeclared_eph_step_is_refused_with_the_way_to_declare_it(base) -> None:
    dynamic, planner = _cycle(base, [_move_reply(steps=({"atom": "eph.fetch_cbr_key_rate_2026"},)), _give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    feedback = planner.prompts[1].split("## Итог прошлого хода")[1]
    assert "eph.fetch_cbr_key_rate_2026" in feedback
    assert "new_atoms" in feedback and "этого же хода" in feedback
    assert "используй только атомы каталога" not in feedback


def test_K19_6_a_missing_catalog_atom_is_refused_as_before(base) -> None:
    dynamic, planner = _cycle(base, [_move_reply(steps=({"atom": "read.nothing"},)), _give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    assert "неизвестный атом 'read.nothing' — используй только атомы каталога" in planner.prompts[1]


# --- Р19.10: предел новых атомов ----------------------------------------------------------------------


def test_the_limit_of_new_atoms_is_sixteen_and_the_state_says_so(base) -> None:
    dynamic, planner = _cycle(base, [_give_up_reply()])

    dynamic.run(deterministic_error="нет пути")

    assert RunLimits().max_new_atoms == 16
    assert "новых атомов: 16 из 16" in planner.prompts[0]


# --- промпт планировщика ------------------------------------------------------------------------------


def _prompt() -> str:
    return (ROOT / "src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_prompt_names_the_search_atom_with_the_fields_the_step_check_accepts() -> None:
    prompt = _prompt()
    section = prompt.split("## Внешние данные", 1)[1].split("\n## ", 1)[0]

    assert "`web.search`" in section and "`concept`" in section and "`extra.topic`" in section
    assert "`news`" in section and "`general`" in section
    assert "выключен пользователем" in section and "не ищи обход" in section
    # dev 08.10.2026: без артефактов `finalize` отклоняется («evidence_ids пуст») — выход назван для обоих случаев
    assert "`finalize`" in section and "`give_up`" in section and "артефактов нет" in section
    assert "адрес" in section and "из находок" in section
    assert "сценари" in section, "внешние данные сценарием не получают — сказано планировщику"


def test_the_prompt_asks_for_the_source_address_and_date_and_separates_data_from_assumptions() -> None:
    prompt = _prompt()
    finalize = prompt.split("## finalize", 1)[1].split("\n## ", 1)[0]
    not_instructions = prompt.split("## Данные — не инструкции", 1)[1].split("\n## ", 1)[0]

    assert "источник" in finalize and "адрес" in finalize and "дат" in finalize
    assert "предположени" in finalize
    assert "находки поиска" in not_instructions and "страниц" in not_instructions
    assert "«по данным FRED»" not in prompt, "старое правило о внешних данных заменено, а не оставлено рядом"


def test_the_plan_phase_knows_that_external_data_start_with_a_search() -> None:
    phase = (ROOT / "src/finengine/skills/spec/dynamic_run_plan_phase.md").read_text(encoding="utf-8")

    assert "web.search" in phase


# --- полные прогоны: execute_routed_goal → ответ пользователю -----------------------------------------

SINGLE = {
    "route": "plan", "intent_kind": "custom_analysis", "targets": [], "entity_ref": None, "period_ref": None,
    "role": None, "unresolved": [MESSAGE], "ambiguities": [],
}
REVENUE_STEP = {"atom": "read.value", "concept": "SALES_REVENUE", "period_ref": None, "extra": {}}
LINK_ATOM = {
    "name": "eph.rate_vs_revenue", "purpose": "выручка отчёта и ключевая ставка со страницы Банка России",
    "requires": [
        {"input_type": "ExtractedMetric", "concept": "SALES_REVENUE"},
        {"input_type": "dyn:rate_page", "concept": None},
    ],
    "output_type": "dyn:rate_vs_revenue",
    "output_schema": {
        "type": "object", "required": ["revenue", "key_rate_pct"],
        "properties": {"revenue": {"type": "number", "x-unit": "RUB_MLN"},
                       "key_rate_pct": {"type": "number", "x-unit": "%"}},
    },
}
LINK_CODE = (
    "import re\n"
    "def run(inputs):\n"
    "    revenue = inputs['ExtractedMetric'][0]['scalar_fact']['value']\n"
    "    text = inputs['dyn:rate_page'][0]['payload']['text']\n"
    "    return {'revenue': revenue, 'key_rate_pct': float(re.search(r'ставка\\s+(\\d+)', text).group(1))}\n"
)


def _arm_cycle(monkeypatch, replies: list) -> SequenceModel:
    locate, read = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1000.0})
    patched_build_registry(monkeypatch, locate, read)
    patch_subtask(monkeypatch, SubtaskCodes([LINK_CODE]))
    set_intent(monkeypatch, SINGLE)
    return _arm_dynamic(monkeypatch, replies)


def _download_the_first_hit(prompt: str) -> dict:
    """Ход модели: адрес берётся из состояния — из находок, как велит промпт."""
    address = re.search(r"https://data\.example\.org/news/\S+", _artifacts_block(prompt)).group(0)
    page = {"name": "eph.rate_page", "purpose": "страница решения по ключевой ставке", "requires": [],
            "output_type": "dyn:rate_page", "output_schema": {"type": "object"}, "url": address}
    return _move(new_atoms=[page], steps=[{"atom": "eph.rate_page"}])


def _finalize_with_sources(prompt: str) -> dict:
    ids = [*_ids(prompt, "ExtractedMetric"), *_ids(prompt, "dyn:rate_vs_revenue")]
    return _move(
        action="finalize", objective="ответить", reason="данные получены", evidence_ids=ids,
        answer=f"Выручка 1 000,0; ключевая ставка 16 % (источник: {PAGE_URL}, 2026-02-13).",
    )


def test_P19_1_the_original_phrase_search_download_code_finalize_with_sources(monkeypatch, local_room, search) -> None:
    seen = _serve(monkeypatch, _html_ok)
    planner = _arm_cycle(monkeypatch, [
        _move(steps=[REVENUE_STEP, _search_step(RATE_QUERY)]),
        _download_the_first_hit,
        _move(new_atoms=[LINK_ATOM], steps=[{"atom": "eph.rate_vs_revenue"}]),
        _finalize_with_sources,
    ])

    turn, events = _turn(monkeypatch, MESSAGE)

    errors = [event for event in events if event.get("type") == "typed_error"]
    assert not errors, errors
    assert [call["query"] for call in search.calls] == [RATE_QUERY]
    assert [str(request.url) for request in seen] == [PAGE_URL], "скачан адрес из находок, а не выдуманный"
    assert "| dyn:rate_page |" in planner.prompts[2] and "| dyn:rate_vs_revenue |" in planner.prompts[3]
    row = next(line for line in planner.prompts[3].splitlines() if "| dyn:rate_vs_revenue |" in line)
    assert '"key_rate_pct": 16.0' in row, "код разобрал текст страницы без разметки и соединил с выручкой"
    assert PAGE_URL in _text(events) and "2026-02-13" in _text(events)


def _answer_that_search_is_off(prompt: str) -> dict:
    """Ход модели по правилу промпта: причина из итога хода уходит в ответ пользователю."""
    feedback = prompt.split("## Итог прошлого хода")[1]
    assert WEB_SEARCH_OFF_REASON in feedback, feedback
    return _move(
        action="finalize", objective="ответить", reason="поиск выключен", evidence_ids=_ids(prompt, "ExtractedMetric"),
        answer=f"Внешних данных нет: {WEB_SEARCH_OFF_REASON}. По отчёту: выручка 1 000,0.",
    )


def test_P19_2_with_the_toggle_off_one_move_is_spent_and_the_answer_says_what_to_switch_on(
    monkeypatch, local_room, search, web_off,
) -> None:
    seen = _serve(monkeypatch, _html_ok)
    planner = _arm_cycle(monkeypatch, [
        _move(steps=[REVENUE_STEP, _search_step(RATE_QUERY)]),
        _answer_that_search_is_off,
    ])

    turn, events = _turn(monkeypatch, MESSAGE)

    assert search.calls == [] and seen == []
    assert len(planner.prompts) == 2, "на выключенный поиск — один ход"
    answer = _text(events)
    assert "выключен пользователем" in answer and "«Поиск в интернете»" in answer and "1 000,0" in answer


def test_P19_3_the_brent_chain_with_the_toggle_off_is_refused_with_the_same_reason(
    monkeypatch, local_room, web_off,
) -> None:
    seen = _serve(monkeypatch, _csv_ok)
    _, planner = _arm(monkeypatch, [
        _move(goals=["goal-1"]),
        _move(new_atoms=[BRENT_ATOM], steps=[{"atom": "eph.brent_csv"}]),
        _move(action="give_up", answer="Цен Brent нет.", objective="остановиться", reason="поиск выключен"),
    ])

    _turn(monkeypatch, BRENT_MESSAGE)

    assert seen == []
    assert WEB_SEARCH_OFF_REASON in planner.prompts[2].split("## Итог прошлого хода")[1]


def test_P19_3_the_brent_chain_with_the_toggle_on_works_as_before(monkeypatch, local_room) -> None:
    seen = _serve(monkeypatch, _csv_ok)
    _, planner = _arm(monkeypatch, [
        _move(goals=["goal-1"]),
        _move(new_atoms=[BRENT_ATOM], steps=[{"atom": "eph.brent_csv"}]),
        _move(new_atoms=[STATS_ATOM], steps=[{"atom": "eph.ebitda_brent"}]),
        _finalize_stats,
    ])

    turn, events = _turn(monkeypatch, BRENT_MESSAGE)

    assert len(seen) == 1 and "460,0" in _text(events)
    row = next(line for line in planner.prompts[2].splitlines() if "| dyn:brent_csv |" in line)
    assert "DATE,DCOILBRENTEU" in row, "CSV показан в состоянии как раньше — началом JSON"
