"""Страница числа доезжает до отчёта — и ссылка не может быть выдуманной (#157).

ЧТО БЫЛО. Движок знает страницу, на которой нашёл число (`page` у метрики), и
она доезжала до таблицы извлечения. До АНАЛИЗА не доезжала: таблица заземления
(`build_metrics_table_str`) отдавала модели только имя, значения, единицу и
sanity-пометку. Сослаться в отчёте было не на что, и проверить число можно было
единственным способом — листая исходный PDF руками.

ЧТО ЗАЩИЩАЕТСЯ ЗДЕСЬ — ровно инвариант, а не удобство: **страницу находит
ДВИЖОК, а не модель**. Модель просят брать номер из таблицы, но послушание
модели гарантией не является: номер можно вспомнить из колонтитула, из
оглавления или просто ошибиться, а такая ссылка ведёт читателя не туда и при
этом выглядит проверенной. Поэтому кликабельность даётся ПО ДАННЫМ: номер,
которого не было в таблице, теряет формат «PDF стр. N» ещё на бэкенде.

Тесты идут ЖИВЫМ путём:
  • разбор — через `get_skill("general").handler` (это и есть то, что зовёт
    станция: `station-skills/aifa-analyze/scripts/analyze.py` → skill.handler);
  • карточка — через `_h_fe_analyze`, как в test_analysis_artifact_provenance;
  • число страниц после облачного OCR — прогоном настоящей фоновой разборки
    документа (`api.main._parse_doc_in_background`) с подменённым OCR-клиентом.
Наружу (сеть, модель, облако) подменяется только то, что физически не может
исполниться в тесте.
"""
from __future__ import annotations

import pytest

import asyncio
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# src-layout: делаем `finengine` импортируемым без editable-установки (как соседние тесты).
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from finengine.llm import openrouter as _or  # noqa: E402
from finengine.skills.analysis import get_skill  # noqa: E402


# Показатели ровно в той форме, в какой их отдаёт движок: у найденного в отчёте
# числа есть страница, у посчитанного формулой — нет.
METRICS = [
    {"enum": "SALES_REVENUE", "name_ru": "Выручка", "value_current": 120_000,
     "value_prev": 100_000, "unit": "млн ₽", "page": 42, "found": True},
    {"enum": "NET_INCOME", "name_ru": "Чистая прибыль", "value_current": 9_000,
     "value_prev": 8_000, "unit": "млн ₽", "page": 43, "found": True},
    {"enum": "EBITDA_MARGIN", "name_ru": "Рентабельность по EBITDA", "value_current": 0.18,
     "value_prev": 0.17, "unit": "", "page": None, "found": True, "is_calculated": True},
]

DOC_TEXT = "===PAGE 1===\nОтчёт о финансовом положении. Выручка 120 000 млн руб.\n"

# Что «написала модель»: одна ссылка на страницу ИЗ таблицы (42) и одна
# выдуманная (7 — такой страницы движок не показывал).
MODEL_ANSWER = """\
## Вердикт

Выручка выросла на 20% (PDF стр. 42), рентабельность стабильна.

## Ключевые показатели

| Показатель | 2024 | 2023 | Δ% / оценка |
|---|---|---|---|
| **Выручка** | 120 000 млн ₽ | 100 000 млн ₽ | +20,0% / **Норма** (рост) |

## Долговая нагрузка

Чистый долг снизился (PDF стр. 7), покрытие процентов комфортное.

## Выводы

1. Рост выручки подтверждён отчётностью (PDF стр. 42).
"""


class _FakeLLM:
    """Модель не вызывается — запоминаем промпт и отдаём заготовленный ответ."""

    def __init__(self, answer: str = MODEL_ANSWER) -> None:
        self.answer = answer
        self.prompt = ""

    async def __call__(self, *, prompt, system=None, model=None, max_output=0):  # noqa: ARG002
        self.prompt = prompt
        return self.answer, {}


def _analyze(metrics, answer: str = MODEL_ANSWER) -> tuple[str, str]:
    """Живой путь разбора: тот же handler, что зовёт станция. Возвращает
    (промпт, который увидела модель; markdown, который увидит пользователь)."""
    fake = _FakeLLM(answer)
    orig = _or.complete_with_usage
    _or.complete_with_usage = fake
    try:
        report = asyncio.run(get_skill("general").handler(
            depth="quick", company_name="ПАО «Пример»", company_type="non_financial",
            standard="ifrs", ocr_text=DOC_TEXT, metrics=metrics, model_id=None,
        ))
    finally:
        _or.complete_with_usage = orig
    return fake.prompt, report


# ── (1) страница движка доезжает до таблицы заземления и до правила в промпте ──


def test_engine_page_reaches_the_grounding_table():
    prompt, _ = _analyze(METRICS)
    assert "[PDF стр. 42]" in prompt, "страница показателя не доехала до таблицы заземления"
    assert "[PDF стр. 43]" in prompt


def _grounding_block(prompt: str) -> str:
    """Кусок промпта с таблицей заземления: от заголовка блока до правила про
    страницу (правило идёт следом и само содержит пример «[PDF стр. 42]»)."""
    start = prompt.index("ЗАЗЕМЛЕНИЕ")
    return prompt[start:prompt.index("📄", start)]


def test_metric_without_a_page_gets_no_page_marker():
    """Рассчитанный коэффициент страницы не имеет — и выдумывать её нельзя."""
    prompt, _ = _analyze([METRICS[2]])
    assert "PDF стр." not in _grounding_block(prompt), \
        "у показателя без страницы появился номер"


def test_prompt_carries_the_verbatim_format_rule():
    """Формат дословный: по нему фронт делает ссылку, а бэкенд — сверку."""
    prompt, _ = _analyze(METRICS)
    rule = prompt[prompt.index("📄"):]
    assert "PDF стр." in rule
    assert "ИЗ ТАБЛИЦЫ" in rule, "правило «бери номер из таблицы» пропало из промпта"
    assert "колонтитул" in rule, "предупреждение про печатную нумерацию пропало"


# ── (2) ГЛАВНОЕ: выдуманная страница ссылкой не становится ───────────────────


def test_page_from_the_table_survives_in_the_report():
    _, report = _analyze(METRICS)
    assert report.count("PDF стр. 42") == 2, "подтверждённая ссылка пропала из отчёта"


def test_invented_page_loses_the_link_format():
    _, report = _analyze(METRICS)
    assert "PDF стр. 7" not in report, "страница, которой движок не находил, осталась ссылкой"
    # Число из рассуждения не выбрасываем — помечаем. Молча стереть строку
    # анализа мы права не имеем; выдать её за проверенную — тем более.
    assert "стр. 7 (страница не подтверждена движком)" in report


def test_without_grounding_the_analysis_does_not_start():
    """Раньше здесь проверялось, что БЕЗ таблицы ни один номер не становится
    ссылкой: движок не отработал — кликать не по чему.

    С 14.08 (#238) такого прогона не существует вовсе. Разбор без таблицы — это
    разбор по цитатам из простыни текста, и страницы в нём были только первой
    из бед. Отказ поднят в `run.run`, до сборки промпта.
    """

    with pytest.raises(ValueError, match="без таблицы показателей"):
        _analyze(None)


# ── (3) карточка знает СВОЙ документ — иначе прыгать некуда ──────────────────


# ── (4) выгрузка: страницу видно в Excel ─────────────────────────────────────


def test_metrics_xlsx_has_the_page_column():
    import openpyxl
    from finengine.skills.metrics_xlsx import build_metrics_xlsx

    rows = [
        {"name_ru": "Выручка", "enum": "SALES_REVENUE", "unit": "млн ₽", "page": 42,
         "period_values": [{"period": "2024", "value": 120_000}]},
        {"name_ru": "Рентабельность по EBITDA", "enum": "EBITDA_MARGIN", "unit": "",
         "page": None, "period_values": [{"period": "2024", "value": 0.18}]},
    ]
    wb = openpyxl.load_workbook(io.BytesIO(build_metrics_xlsx(rows)))
    ws = wb["Показатели"]
    header = [c.value for c in ws[1]]
    assert header[-1] == "Стр. PDF", f"колонки страницы нет: {header}"
    assert ws.cell(row=2, column=len(header)).value == 42
    # Посчитанный коэффициент страницы не имеет — пусто, а НЕ ноль: ноль
    # прочитался бы как номер страницы.
    assert ws.cell(row=3, column=len(header)).value in ("", None)


# ── (5) после облачного OCR документ знает число страниц ─────────────────────


def test_idp_ocr_fills_total_pages():
    """Без него лента страниц не собирается и прыгать физически некуда.

    Гоняем НАСТОЯЩУЮ фоновую разборку документа, подменяя только облачный
    вызов: страницы считает PyMuPDF внутри клиента, и в `diag["pages_total"]`
    лежит ФИЗИЧЕСКОЕ число страниц файла.
    """
    from finengine.api import main as _m
    from finengine.skills.aifa_baseline import idp_ocr_client as _idp

    saved: dict = {}
    meta = SimpleNamespace(doc_id="doc-idp", filename="о.pdf", company_name="ПАО «Пример»",
                           total_pages=0, ocr_engine="", catalog_file_id="")

    class _Index:
        def get_document(self, _doc_id):
            return meta

        def add_document(self, m, _chunks):
            saved["meta"] = m

    async def _fake_ocr(_path, save_raw_to=None, parallel=1):  # noqa: ARG001
        # 7 физических страниц, одна из которых не распозналась: нумерация
        # ленты идёт по ФАЙЛУ, поэтому здесь именно pages_total.
        return SimpleNamespace(
            full_text="===PAGE 1===\nтекст\n\n===PAGE 2===\nтекст\n",
            diag={"pages_total": 7, "pages_success": 6, "pages_failed": 1},
        )

    orig = (_m.get_store, _idp.call_idp_ocr, _m.settings)
    with tempfile.TemporaryDirectory() as td:
        _m.get_store = lambda _х=_Index(): _х
        _idp.call_idp_ocr = _fake_ocr
        _m.settings = SimpleNamespace(docs_dir=Path(td), idp_ocr_parallel=1)
        try:
            # 🔴 ПОРЯДОК АРГУМЕНТОВ СМЕНИЛСЯ 25.08: первым идёт ССЫЛКА НА ФАЙЛ
            # (ключ объекта или путь), а не doc_id. Разбор теперь берёт файл
            # через `document_file`, поэтому файл обязан существовать —
            # раньше путь был декоративным, его открывал только заглушенный OCR.
            пдф = Path(td) / "о.pdf"
            пдф.write_bytes(b"%PDF-1.4 stub")
            _m._parse_doc_in_background(str(пдф), "doc-idp", "о.pdf", "idp")
        finally:
            _m.get_store, _idp.call_idp_ocr, _m.settings = orig

    assert saved.get("meta") is not None, "метаданные документа не сохранились"
    assert saved["meta"].total_pages == 7, \
        "после облачного OCR документ по-прежнему без числа страниц"
