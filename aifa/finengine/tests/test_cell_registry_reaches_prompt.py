"""Реестр ЯЧЕЙКИ доезжает до промпта извлечения (#147).

ЧТО БЫЛО. `data/registry/<ячейка>.json` объявлен редактируемой копией аналитика
(SKILL.md: «правь под свою отчётность, ядро не трогается»). Но живой путь
(чат → fe_extract_metrics → станция → движок) читал из этого файла ТОЛЬКО список
enum'ов для фильтра, а сами строки показателей (name_ru/aliases/description/hint)
и формулы расчётов брал из `data/metrics.json` — файла, сгенерированного скриптом
из Excel. Следствия: правка синонима или подсказки в ячейке игнорировалась, а
шесть коэффициентов, заведённых #137 только в файлы ячеек, не считались НИКОГДА.

Почему это прожило до прода: юнит-тест #137 (`test_analysis_coefficients_covered`)
ходит в файл ячейки НАПРЯМУЮ и был зелёным при сломанном живом пути. Поэтому
здесь проверяется именно ЦЕПОЧКА — от вызова в станции до строк, которые движок
реально отдаёт.

ЗАКРЫТО (Срез 13.5): статическая проверка `test_station_extract_passes_the_
cell_registry` больше не грепает `aifa-extract/scripts/extract.py` — тонкий
станционный скрипт больше не вызывает `extract_stream_from_text(...)` вовсе
(живой путь ушёл в граф: `read.value`/`locate`, `graph_atoms.py`). Цепочка
«реестр ячейки доезжает до промпта» осталась настоящей и обязательной, просто
её сегодняшнее звено — `_read_value_batch_real`/`_locate_batch_real`, и
проверяется теперь оно.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from finengine.skills.atoms import graph_atoms
from finengine.skills.bulk_extractor import extract_stream_finengine_dev
from finengine.skills.metric_schema import registry_for_profile

REPO = Path(__file__).resolve().parents[1]
STATION_ANALYZE = (REPO / "station-skills" / "aifa-analyze" / "scripts" / "analyze.py").read_text(
    encoding="utf-8")

# Шесть коэффициентов #137: заведены формулами в ячейки, в metrics.json их нет.
# Ровно они и терялись, пока реестр ячейки до движка не доезжал.
NF_LOST = {"QUICK_RATIO", "WORKING_CAPITAL", "OCF_TO_EBITDA"}
BANK_LOST = {"ROA", "ASSET_YIELD", "FUNDING_COST"}


# ── статика: живой путь обязан передавать реестр ────────────────────────────

@pytest.mark.parametrize("real_batch_fn, batch_fn_name", [
    (graph_atoms._locate_batch_real, "locate_batch"),
    (graph_atoms._read_value_batch_real, "read_value_batch"),
])
def test_station_extract_passes_the_cell_registry(real_batch_fn, batch_fn_name):
    src = inspect.getsource(real_batch_fn)
    assert "registry_for_passport(" in src, (
        f"{real_batch_fn.__name__}: узел графа не берёт реестр по frozen "
        "паспорту — правки в промпт не доедут")
    assert f"{batch_fn_name}(" in src and "metrics=metrics" in src, (
        f"{real_batch_fn.__name__}: реестр не передан в {batch_fn_name} — "
        "прочитан, но не донесён до промпта")


def test_station_analyze_has_no_engine_call_of_its_own():
    """Здесь стояла парная проверка «анализ тоже передаёт реестр ячейки».

    С 14.08 (#238) передавать нечего: своего прохода движка у анализа нет,
    таблицу с коэффициентами он получает готовой от прогона извлечения. Реестр
    ячейки доезжает до движка ОДИН раз — проверкой выше. Оставить здесь вызов
    значило бы вернуть второй, независимый проход по тому же отчёту.
    """
    assert "extract_stream_from_text(" not in STATION_ANALYZE, \
        "у анализа снова свой проход движка — таблица и вердикт разойдутся"


def test_no_second_prompt_assembly_left():
    """Матрица per-cell экстракторов снесена ФИЗИЧЕСКИ, а не «на всякий случай».

    Она делала правильную вещь (передавала registry_override), но её никто не
    вызывал — живой путь ушёл в станцию. Оставить рядом второй, неисполняемый
    сборщик промпта = гарантировать повторение (урок #99).
    """
    cells = REPO / "src" / "finengine" / "skills" / "extractors"
    assert not (cells / "_base.py").exists(), "диспетчер мёртвой матрицы вернулся"
    leftovers = sorted(p.name for p in cells.glob("_ifrs_*.py"))
    leftovers += sorted(p.name for p in cells.glob("_us_gaap_*.py"))
    assert not leftovers, f"вторая сборка промпта вернулась: {leftovers}"


# ── функционально: движок реально считает по реестру ячейки ─────────────────

class _FakeStream:
    """Модель отвечает пустым JSON: значения нам не нужны, проверяем СОСТАВ
    строк, которые движок выдаёт (расчётная строка эмитится для каждой формулы
    реестра независимо от того, нашлись ли входы)."""

    async def __call__(self, *_a, **_kw):
        yield "{}"


def _run(profile_id: str, company_type: str, registry_override) -> set[str]:
    from finengine.skills import bulk_extractor as be

    reg = registry_for_profile(profile_id)
    wanted = {m["enum"] for m in reg["extractable"]} | {c[0] for c in reg["calculated"]}

    class _Doc:
        company_name = "Тест"
        filename = "test.pdf"
        period = "2024"

    class _Index:
        def get_document(self, doc_id):  # noqa: ARG002
            return _Doc()

        def get_chunks_by_type(self, kind, doc_id=None):  # noqa: ARG002
            return []

    from finengine.config import settings
    docs = Path(settings.docs_dir)
    docs.mkdir(parents=True, exist_ok=True)
    doc_id = f"test-{profile_id}"
    (docs / f"{doc_id}.txt").write_text("===PAGE 1===\nотчёт\n", encoding="utf-8")

    async def go() -> set[str]:
        out: set[str] = set()
        async for ev in extract_stream_finengine_dev(
            doc_id, company_type, index=_Index(), metric_enums=wanted,
            include_segments=False, include_consolidated_group=False,
            registry_override=registry_override,
        ):
            if ev.get("type") == "metric" and ev.get("is_calculated"):
                out.add(ev["enum"])
        return out

    orig = be.llm.stream
    be.llm.stream = _FakeStream()
    try:
        return asyncio.run(go())
    finally:
        be.llm.stream = orig
        (docs / f"{doc_id}.txt").unlink(missing_ok=True)


@pytest.mark.parametrize(
    "profile_id, company_type, lost",
    [("ifrs_nf_ru", "non_financial", NF_LOST), ("ifrs_bank_ru", "bank", BANK_LOST)],
)
def test_engine_computes_the_cells_own_formulas(profile_id, company_type, lost):
    emitted = _run(profile_id, company_type, registry_for_profile(profile_id))
    assert lost <= emitted, f"формулы ячейки не доехали до движка: {sorted(lost - emitted)}"


@pytest.mark.parametrize(
    "profile_id, company_type, lost",
    [("ifrs_nf_ru", "non_financial", NF_LOST), ("ifrs_bank_ru", "bank", BANK_LOST)],
)
def test_without_the_cell_registry_the_engine_still_uses_a_cell(profile_id, company_type, lost):
    """🔴 СМЫСЛ ЭТОГО ТЕСТА ПЕРЕВЁРНУТ 21.08.2026, И ЭТО НЕ ОПЕЧАТКА.

    Раньше он назывался «…those formulas are gone» и охранял ГРАНИЦУ ДЕФЕКТА:
    без реестра ячейки движок брал формулы из `data/metrics.json`, где шести
    коэффициентов нет вовсе. Дефект чинился передачей ячейки, а тест следил,
    чтобы его не «починили» досыпкой формул во вторую копию.

    Дефекта больше нет. Запасной путь ведёт не во вторую копию, а в ЯЧЕЙКУ ПО
    УМОЛЧАНИЮ (`ifrs_bank_ru` / `ifrs_nf_ru`). Замерено перед правкой: легаси
    оказался строгим ПОДМНОЖЕСТВОМ ячейки — ни одного показателя, которого нет
    в ячейке, — а формулы у общих расходились у 12 записей банка и 2
    нефинансовой. После перевода доходностей в проценты расхождение стало
    стократным.

    Поэтому теперь охраняется ОБРАТНОЕ: даже без явной ячейки движок считает
    формулами ячейки. Вернётся чтение `metrics.json` — тест покраснеет.

    ⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ, проверено мутацией 21.08: он гоняет ЗАПАСНОЙ
    движок (`bulk_extractor/streams_full_context`). Тот же возврат к
    `metrics.json` в БОЕВОМ (`two_stage_extractor`) он оставляет зелёным —
    туда `_run` не заходит вовсе. Боевой запасной путь сегодня не застережён
    никем: чтобы его проверить, нужен прогон двухстадийной схемы БЕЗ ячейки, а
    такого сценария в наборе нет.
    """
    emitted = _run(profile_id, company_type, None)
    assert lost <= emitted, (
        f"без явной ячейки движок недосчитал {sorted(lost - emitted)} — значит "
        "запасной путь снова ведёт во вторую копию реестра, а не в ячейку")
