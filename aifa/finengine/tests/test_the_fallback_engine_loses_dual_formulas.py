"""Откат «одной переменной» теряет формулы двойной природы — и это сказано вслух.

ГДЕ РАСХОЖДЕНИЕ. Боевой `two_stage_extractor` с 19.08 держит обе половины
показателя, заведённого И извлекаемым, И расчётным: строку нашли в отчёте —
формула молчит, не нашли — считает (`_dual_nature_enums` + `deferred_dual`).
Запасной `streams_full_context` этой правки НЕ получил: там остался безусловный
снос Finengine — расчётная половина выбрасывается ДО прогона.

ПОЧЕМУ ЭТО НЕ ТЕОРИЯ. `config.py` объявляет `FINENGINE_EXTRACT_ARCH=full_context`
штатным откатом на случай жалоб на пустые клетки. То есть аварийный режим чинит
одно и молча теряет другое — в ячейке `ifrs_nf_ru` это шесть формул, включая
EBITDA, CAPEX и валовую прибыль.

ЗАЧЕМ СТОРОЖ НА ТО, ЧТО НЕ ЧИНИМ. Отчёт ветки `work/c4-deterministic-sums`
объявил доставку формул доказанной, не назвав этого второго пути. Комментарий,
объясняющий потерю, теперь стоит в `streams_full_context` — а комментарий без
сторожа переживает свой повод и начинает врать. Довезут правило — этот файл
покраснеет и скажет: абзац про потерю пора убрать вместе с ним.

⚠️ ПЕРЕПИСАН 21.08.2026 (ревизия набора). До этого дня файл читал ИСХОДНИК
запасного движка и искал в нём имена `extracted_enum_set` / `deferred_dual`.
Такая проверка держится на написании, а не на счёте: переименуй переменную —
покраснеет без всякой поломки, обойди фильтр другим кодом под тем же именем —
не заметит. Теперь движок ЗАПУСКАЕТСЯ на реестре ячейки, и потеря считается по
его собственному событию `start`.

ЧЕГО СТОРОЖ НЕ ЛОВИТ: он доводит движок только до `start` — всё, что запасной
путь делает ПОСЛЕ (промпт, разбор ответа модели, сборка строк), здесь не
проверяется вовсе.
"""
from __future__ import annotations

import asyncio

from finengine.config import settings
from finengine.skills.bulk_extractor.streams_full_context import (
    _extract_stream_full_context,
)
from finengine.skills.metric_schema import registry_for_profile

CELL = "ifrs_nf_ru"


class _Doc:
    doc_id = "d1"
    period = "2024"
    company_name = "Северсталь"
    filename = "severstal-2024.pdf"


class _Index:
    """Хранилище ровно в том объёме, в каком его трогает запасной движок."""

    def get_document(self, doc_id: str) -> _Doc:
        return _Doc()

    def get_chunks_by_type(self, kind: str, doc_id: str | None = None) -> list:
        return []


async def _start_event(registry: dict) -> dict:
    """Первое событие прогона: в нём движок объявляет, СКОЛЬКО показателей взял.

    До обращения к модели дело не доходит — `start` уходит раньше промпта.
    """
    gen = _extract_stream_full_context(
        "d1", "non_financial", index=_Index(), registry_override=registry,
    )
    try:
        async for event in gen:
            if event.get("type") == "start":
                return event
    finally:
        await gen.aclose()
    raise AssertionError("запасной движок не дошёл до события start")


def test_the_fallback_engine_still_drops_dual_formulas(tmp_path, monkeypatch):
    """Прогон запасного движка теряет РОВНО расчётные половины двойных пар."""
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    (tmp_path / "d1.txt").write_text(
        "===PAGE 1===\nВыручка 100\nEBITDA 20\n", encoding="utf-8")

    registry = registry_for_profile(CELL)
    extractable = {str(m.get("enum", "")).strip().upper()
                   for m in registry["extractable"]}
    calculated = [str(row[0]).strip().upper() for row in registry["calculated"]]
    dual = sorted(extractable & set(calculated))
    assert dual, (
        f"в ячейке {CELL} не осталось показателей двойной природы — терять "
        "нечего, и сторож проверяет пустоту. Проверьте реестр")

    event = asyncio.run(_start_event(registry))
    survived = event["total_metrics"] - event["total_extractable"]

    assert survived == len(calculated) - len(dual), (
        f"запасной движок оставил {survived} формул из {len(calculated)}, "
        f"а двойных пар {len(dual)} ({dual}). Если правило «извлечённое старше "
        "расчётного» довезли и сюда — удалите этот файл ВМЕСТЕ с абзацем про "
        "потерю формул в `streams_full_context`: иначе комментарий переживёт "
        "свой повод и начнёт врать")
    assert survived < len(calculated), (
        "формулы двойной природы больше не теряются — см. сообщение выше")
