"""RAG-путь извлечения снят и не возвращается (13.08.2026).

ЗАЧЕМ ЭТОТ ФАЙЛ. `arch="finengine_rag"` — per-metric FAISS retrieval → LLM —
держал в `bulk_extractor.py` 1 475 строк из 6 445: три словаря собственной
методики (`METRIC_ALIASES_EXTRA`, `METRIC_EXPANSION_ALIASES`,
`METRIC_PROMPT_HINTS`) и одиннадцать функций, которыми пользовался только он.

Опасность была не в объёме, а в том, что это ВТОРАЯ методика извлечения. Живой
путь (`_extract_stream_full_context`) берёт хинты и синонимы из реестра
`data/registry/<ячейка>.json`, RAG-путь — из своих словарей. Две методики уже
разошлись, и правка одной молча не доезжала до второй. Владелец 13.08 решил
снести; то же самое сделано в Finengine и записано правилом
`docs/rules/architecture.md` 4.7: «`arch=finengine_rag` УДАЛЁН … Не возрождать».

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ:
  1. трёх словарей в модуле нет;
  2. одиннадцати RAG-функций в модуле нет;
  3. функции, которые RAG звал, но которыми пользуется и живой путь, — НА МЕСТЕ
     (это сторож от того, что снос заодно вырежет общий хелпер);
  4. API отвечает на `arch=finengine_rag` ГРОМКО (400), а не проваливается
     молча на full-context: иначе старый клиент получил бы другой движок под
     прежним именем;
  5. фронт больше не предлагает этот режим, но подпись для ИСТОРИИ прогонов
     сохранена, и сохранённый выбор мигрируется.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ДЕЛАЕТ: он не проверяет качество извлечения. Снос не
менял живой путь ни в одной строке — доказательство этому в том, что 1 156
тестов остались зелёными, а не в этом файле.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `bulk_extractor.py` любой из снятых
символов — тест 1 или 2 покраснеет. Заменить `raise HTTPException` в ветке
`finengine_rag` на прежний импорт — покраснеет тест 4. Вернуть пункт в
выпадашку композера — тест 5.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Пакет целиком, а не один файл: движок разбирается на модули, и сторож,
# привязанный к `bulk_extractor.py`, перестал бы сторожить ровно тогда,
# когда код переедет в подмодуль, — молча и зелёным.
EXTRACTOR = ROOT / "src" / "finengine" / "skills" / "bulk_extractor"
API = ROOT / "src" / "finengine" / "api" / "main.py"
PREFS = ROOT / "web" / "src" / "lib" / "prefs.ts"

REMOVED_DICTS = ("METRIC_ALIASES_EXTRA", "METRIC_EXPANSION_ALIASES", "METRIC_PROMPT_HINTS")
REMOVED_FUNCS = (
    "_extract_stream",
    "_retrieve_chunks_for_metric",
    "_build_metric_prompt",
    "_call_claude_metric",
    "_detect_company_type_from_chunks",
    "_detect_unit",
    "_detect_periods",
    "_safe_num",
    "_pick_two_values",
    "_pick_values_and_unit",
    "_is_anchor_report_period",
)
# Их RAG тоже звал, но зовёт и живой путь — вырезать было НЕЛЬЗЯ.
KEPT_SHARED = (
    "_calculate_metrics_pair",
    "_normalise_money_value",
    "_sanity_note",
    "unit_for_calculated",
    "_extract_stream_full_context",
    "_extract_stream_unrestricted",
)


@pytest.fixture(scope="module")
def extractor_src() -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(EXTRACTOR.rglob("*.py")))


def test_rag_dictionaries_are_gone(extractor_src: str) -> None:
    """1. Вторая методика извлечения удалена вместе с путём."""
    for name in REMOVED_DICTS:
        assert not re.search(rf"^{name}\s*[:=]", extractor_src, re.M), (
            f"{name} вернулся в bulk_extractor.py — это вторая методика извлечения "
            "рядом с реестром, из-за неё правки молча не доезжают"
        )


def test_rag_functions_are_gone(extractor_src: str) -> None:
    """2. Функции, которыми пользовался только RAG-путь."""
    for name in REMOVED_FUNCS:
        assert not re.search(rf"^(async )?def {name}\(", extractor_src, re.M), (
            f"{name} вернулась в bulk_extractor.py — см. architecture.md 4.7"
        )


def test_shared_helpers_survived(extractor_src: str) -> None:
    """3. Общие хелперы на месте — снос не должен был их задеть."""
    for name in KEPT_SHARED:
        assert re.search(rf"^(async )?def {name}\(", extractor_src, re.M), (
            f"{name} исчезла — её зовёт ЖИВОЙ путь извлечения, а не только RAG"
        )


def test_no_route_lets_a_client_pick_an_extraction_engine() -> None:
    """4. Попросить движок по имени НЕЛЬЗЯ — просить больше некуда.

    🔴 СТОРОЖ ПЕРЕПИСАН 29.08.2026, ВМЕСТЕ С ПРЕДМЕТОМ. Он проверял, что ветка
    `elif arch == "finengine_rag"` отвечает громким 400, а не проваливается в
    `else` на другой движок. Ветка жила внутри роута
    `POST /documents/{doc_id}/extract/stream`, а роут снят: он запускал
    извлечение В ПРОЦЕССЕ БЭКЕНДА, минуя станцию, — один документ извлекался
    двумя способами в двух местах, и правка навыка доезжала только до одного.

    Опасение сторожа теперь закрыто УСТРОЙСТВОМ, а не проверкой: выбирать движок
    негде. Поэтому проверяется новое — что дверь не вернулась и что ни один роут
    не принимает имя движка отдельным полем запроса.
    """
    src = API.read_text(encoding="utf-8")
    assert '@app.post("/documents/{doc_id}/extract' not in src, (
        "вернулся прямой роут извлечения. Он считает в процессе бэкенда, мимо "
        "станции и её полки: правка навыка до него не доезжает, паспорта правил "
        "у него нет. Извлечение идёт ходом в чат — и только им"
    )
    assert "bulk_extractor import _extract_stream as" not in src, (
        "импорт снятого RAG-стрима вернулся в main.py"
    )
    assert 'arch == "finengine_rag"' not in src, (
        "в main.py снова выбирают движок извлечения по имени из запроса"
    )


def test_frontend_dropped_the_option_but_kept_history_label() -> None:
    """5. Выбора нет, подпись истории есть, сохранённый выбор мигрируется.

    Композер старого экрана — место, где жила выпадашка режима, — снесён
    вместе с экраном (Р20, 01.09.2026): выпадашки нет вместе с носителем.
    Живым остался контракт prefs — тип, миграция и подпись истории.
    """
    prefs = PREFS.read_text(encoding="utf-8")
    assert '| "finengine_rag"' not in prefs, "значение вернулось в тип ExtractionArch"
    assert '=== "finengine_rag"' in prefs, (
        "снята миграция сохранённого выбора: у того, кто пользовался режимом, "
        "значение лежит в localStorage и переживёт правку"
    )
    assert 'case "finengine_rag":' in prefs, (
        "снята подпись для ИСТОРИИ прогонов — у старых прогонов в базе стоит "
        "именно этот arch, страница покажет их без названия"
    )
