"""Сорвавшийся батч второй стадии виден в ИСХОДЕ прогона, а не только в диагностике.

ЧТО КУПИЛО ЭТОТ ФАЙЛ (разбор проверяющего, 20.08.2026).
До деления второй стадии на батчи вызов был ОДИН: его срыв означал пустую
ячейку целиком, и это ловил `refusal_reason` — «извлечение провалилось
целиком», ненулевой код возврата, модель докладывает провал. С делением на
восемь батчей срыв ОДНОГО из них перестал быть отказом: строки остальных семи
непусты, значит `refusal_reason` молчит, скрипт станции выходит нулём, а
человек получает таблицу с дырой ровно в состав сорвавшегося батча и читает её
как результат. Проверено исполнением (`test_a_failed_batch_is_not_a_refusal`):
семь провалившихся батчей из восьми и десять найденных значений из восьмидесяти
дают ПУСТУЮ причину отказа.

ПОРОГА ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО. Сколько сорвавшихся батчей делают прогон
негодным — решение владельца, а не побочный эффект правки скорости (тот же
довод записан в докстринге `refusal_reason`). Этот файл стережёт РОВНО
видимость: факт потери обязан доехать до данных прогона и до сводки, откуда его
прочитают модель и человек. Молчаливая дыра — тот же класс дефекта, ради
которого написан `refusal_reason`, только градусом ниже.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ — мутации, прогнанные 20.08.2026:
* `shard_losses` возвращает `{}` всегда → красны четыре проверки видимости;
* `batches_failed` считает `stage2_batch_count` вместо `stage2_invalid_batches`
  → красны прогонные проверки (имена ключей диагностики держатся прогоном, а не
  списком в тесте);
* убрать `**({"losses": losses} ...)` из `result` в скрипте станции → красна
  `test_the_station_outcome_carries_the_losses`;
* убрать `"losses"` из `_result_for_screen` → красна
  `test_the_turn_summary_carries_the_losses`.
"""
from __future__ import annotations

import asyncio
import json

from finengine.config import settings
from finengine.skills import extract_text as et
from finengine.skills import two_stage_extractor as ts
from finengine.skills.extract_result import refusal_reason, shard_losses
from finengine.skills.metric_schema import registry_for_profile

_DOC_WITH_TABLE = (
    "===PAGE 1===\n"
    "Отчёт о финансовом положении\n\n"
    "| Показатель | 2024 | 2023 |\n"
    "| --- | --- | --- |\n"
    "| Итого активы | 1 000 | 900 |\n"
)


def _run_with_one_batch_broken(monkeypatch, profile_id: str = "ifrs_nf_ru") -> tuple[dict, list[dict]]:
    """Настоящий прогон, где ОДИН батч отвечает мусором на все попытки.

    Подделан ровно шлюз модели: имена ключей диагностики (`stage2_batch_count`,
    `stage2_invalid_batches`) обязаны держаться прогоном движка, а не списком в
    тесте — переименуй их в движке, и `shard_losses` молча вернёт нули.
    """
    registry = registry_for_profile(profile_id)
    enums = [str(m["enum"]).upper() for m in registry["extractable"]]
    targets: dict[str, list[str]] = {}
    broken: dict[str, list[str]] = {}
    real_build = ts._build_main_extraction_prompt

    def _spy_build(**kw):
        text = real_build(**kw)
        targets[text] = [str(m["enum"]).upper() for m in kw["target_metrics"]]
        return text

    async def _fake_llm(*, prompt, system=None, model=None, response_format=None,
                        max_output=None, omit_max_output=None, **kw):
        asked = targets.get(prompt)
        if asked is None:
            return json.dumps({"metrics": {
                enum: {"tables": [], "quotes": [{"page": 1, "text": "Итого активы 1 000"}]}
                for enum in enums
            }}), {}
        if enums[0] in asked:
            broken["enums"] = list(asked)
            return "не JSON, а извинения модели", {}
        return json.dumps({
            enum: {"values": {"2024": 1000.0}, "unit_text": "тыс. руб.", "page": 1}
            for enum in asked
        }), {}

    monkeypatch.setattr(ts, "_build_main_extraction_prompt", _spy_build)
    monkeypatch.setattr(ts.llm, "complete_with_usage", _fake_llm)

    diag: dict = {}
    rows: list[dict] = []

    async def _run() -> None:
        async for event in et.extract_stream_from_text(
            _DOC_WITH_TABLE,
            company_type="bank" if "bank" in profile_id else "non_financial",
            metric_enums=set(enums),
            registry_override=registry,
        ):
            if event.get("type") == "metric" and not event.get("dimension"):
                rows.append(event)
            elif event.get("type") == "done":
                diag.update(event.get("_diag") or {})

    asyncio.run(_run())
    diag["_broken_enums"] = broken.get("enums") or []
    return diag, rows


def test_a_failed_batch_is_not_a_refusal(monkeypatch):
    """ГЛАВНАЯ ПРАВДА ФАЙЛА: срыв батча прогон отказом НЕ делает.

    Это не претензия к `refusal_reason` — порог частичного провала решает
    владелец. Это причина, по которой потеря обязана быть названа отдельно:
    иначе её не назовёт никто.
    """
    diag, rows = _run_with_one_batch_broken(monkeypatch)
    assert len(diag["stage2_invalid_batches"]) == 1, diag["stage2_invalid_batches"]
    assert diag["stage2_batch_count"] > 1
    assert refusal_reason(rows, diag) == "", (
        "срыв одного батча объявлен отказом — порог придумали за владельца")


def test_a_failed_batch_costs_exactly_its_own_metrics(monkeypatch):
    """Цена срыва одного батча — числом, а не на глаз: сколько строк осталось пустыми.

    ⚠️ Ровно `extract_batch_size` здесь не требуется, и до 20.08 требовалось зря:
    состав делится не нацело, и хвостовые батчи короче на один показатель. При
    делении по порядку реестра полным был ПЕРВЫЙ батч, и равенство держалось
    случайно; балансировка по весу решает, кому достанется короткий батч, и
    сторож покраснел, не поймав ни одного дефекта. Проверяем то, что и правда
    обязано быть: цена — ровно ОДИН батч, не половина и не два.
    """
    diag, rows = _run_with_one_batch_broken(monkeypatch)
    lost = {str(row["enum"]).upper() for row in rows if not row.get("found")}
    assert set(diag["_broken_enums"]) <= lost, "показатели сорвавшегося батча всё-таки нашлись"
    asked = len(registry_for_profile("ifrs_nf_ru")["extractable"])
    count = diag["stage2_batch_count"]
    assert asked // count <= len(diag["_broken_enums"]) <= settings.extract_batch_size, (
        f"в сорвавшемся батче {len(diag['_broken_enums'])} показателей при "
        f"{asked} на {count} батчей — это не один батч")


def test_the_loss_is_named_in_the_outcome(monkeypatch):
    """Тот же прогон, но глазами исхода: потеря названа числом сорвавшихся батчей."""
    diag, _rows = _run_with_one_batch_broken(monkeypatch)
    losses = shard_losses(diag)
    assert losses["batches"] == diag["stage2_batch_count"] > 1
    assert losses["batches_failed"] == 1, (
        f"сорвавшийся батч не назван в исходе: {losses}")


def test_unasked_metrics_are_named_not_just_counted():
    """Показателей, которых не спрашивали, мало знать числом — нужны ИМЕНА.

    В таблице они выйдут пустыми и неотличимо от «в отчёте нет». Отличить их
    может только тот, у кого есть список.
    """
    losses = shard_losses({
        "stage2_batch_count": 8,
        "stage2_invalid_batches": [],
        "stage2_skipped_empty_evidence": ["EQUITY", "NET_DEBT"],
    })
    assert losses["metrics_unasked"] == ["EQUITY", "NET_DEBT"]
    assert losses["metrics_unasked_total"] == 2


def test_a_schema_without_batches_says_nothing():
    """У полноконтекстной схемы батчей нет вовсе — и нулей о них быть не должно.

    `{"batches": 0, "batches_failed": 0}` читалось бы как «шардировали и не
    потеряли», а на деле не шардировали вовсе.
    """
    assert shard_losses(None) == {}
    assert shard_losses({}) == {}
    assert shard_losses({"llm_requests": [], "wall_s": 12.0}) == {}


# ⚠️ УДАЛЕНЫ Срезом 13.5.4: `test_the_station_outcome_carries_the_losses` и
# `test_the_turn_summary_carries_the_losses`.
#
# Оба проверяли, что `result`/`_result_for_screen` (станционный словарь и
# сводка хода СТАРОГО двухстадийного движка) несут `losses` —
# `shard_losses(diag)`, посчитанный по `_diag`/`stage2_*` того же движка.
# Канонический `station-skills/aifa-extract/scripts/extract.py` больше не
# строит `result`-словарь такой формы вообще (thin shell над `CaseRuntime`/
# `PlanGraph`, `_diag` ему не виден — он внутренний для graph atoms
# `read.value`/`locate`), а `station_turn._result_for_screen` — мёртвый код,
# физически удалённый вместе с недостижимыми телами `_finish_run`/
# `_finish_analysis` тем же срезом.
#
# `shard_losses`/`refusal_reason` САМИ ПО СЕБЕ остаются рабочими чистыми
# функциями (тесты выше, до этой отметки, не удалены и зелёные) — удалён
# только мост «диагностика движка → исход прогона», которого для
# канонического пути не существует физически, не по недосмотру.
#
# РЕАЛЬНЫЙ ПРОБЕЛ, НЕ ЗАМОЛЧАННЫЙ: посрочный разбор ПОТЕРЬ ШАРДИРОВАНИЯ
# (какой конкретно батч сорвался и какие показатели он нёс) не имеет
# аналога на каноническом пути — `read.value`/`locate` не эмитят такую
# диагностику наружу станционного скрипта вовсе. Зафиксировано в
# `Nedodelano.md` («Срез 13.5.4 — видимость потерь шардирования утеряна на
# каноническом пути») как отдельная работа по инструментации atoms, вне
# мандата этого среза (владелец слушателя станции, не atom-уровня).
