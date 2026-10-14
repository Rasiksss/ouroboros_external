"""Свои определения человека переживают комнату и возвращаются к нему (Р1).

🔴 ЗАЧЕМ. Файлы `--extra-metrics` и `--extra-calculated` пишет модель В КОМНАТЕ
станции, а комната живёт двадцать минут (`grounding_bus.WORKER_LIFETIME_SEC`).
Следующим ходом человек говорит «поищи это другими словами» или «на первом шаге
ты написала A/B, а нужно A/(B+C)» — и править НЕЧЕГО: ни описаний, ни формул уже
нет нигде. Модель сочиняет заново, с нуля, каждый раз.

Владелец назвал это работой №1 и сформулировал так: «чтобы это не было
безвозвратно стёртое, чтобы он мог вернуться на втором шаге, скорректировать
описание хинтов и запустить новый цикл».

ЦЕПОЧКА ЦЕЛИКОМ (все три звена обязаны работать, иначе петли нет):
  1. станция кладёт ПРИНЯТОЕ в конверт прогона — `own_metrics` (extract.py) и
     `own_calculated` (calc_run.py). Именно принятое, а не поданное: отвергнутая
     формула в подсказку попасть не должна;
  2. конверт сохраняет хранилище исходов (`station_run.payload`) — он переживает
     и комнату, и перезапуск бэкенда;
  3. следующий ход показывает их модели блоком в промпте.

⚠️ ТУЛА ДЛЯ ЭТОГО НЕ ЗАВОДИМ СОЗНАТЕЛЬНО. Блок в промпте дешевле и не добавляет
модели решения, которого она не просила: подсказка приезжает сама, спрашивать её
не нужно.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 24.08.2026, каждая покраснела):
  · станция перестаёт класть своё в конверт → падает `test_the_station_puts…`;
  · читатель возвращает пусто при непустом конверте → падает `test_the_next_turn…`;
  · отказ хранилища роняет ход вместо тишины → падает `test_a_broken_store…`.
"""
from __future__ import annotations

import re
from pathlib import Path


from finengine.orchestrator.agent import _own_definitions_for

КОРЕНЬ = Path(__file__).resolve().parents[1]


class _Outcome:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


def _store(monkeypatch, payload: dict | None) -> None:
    class _Store:
        def latest_for_doc(self, doc_id, *, user_id, kind=""):  # noqa: ANN001
            return _Outcome(payload) if payload is not None else None

    monkeypatch.setattr("finengine.store.runs.get_run_store", lambda: _Store())


# ⚠️ Срез 13.5.4: `test_the_station_puts_its_own_definitions_into_the_envelope`
# (звено 1, extract.py-половина) удалено. Канонический
# `station-skills/aifa-extract/scripts/extract.py` больше не принимает
# `--extra-metrics`/`--extra-calculated` вовсе — оба флага дают
# `NotImplementedError`/typed refusal (`station ad-hoc definition files are
# not a canonical Case definition source`), а не пишут `own_metrics` в
# конверт: свои показатели человека теперь заводятся ТОЛЬКО через
# `EXTRACT_AD_HOC_METRIC`/`CALCULATE_AD_HOC_METRIC` ingress
# (`AdHocConceptSpec`/`AdHocFormulaSpec`, живут в `Case`, `02-tekushee-
# sostoyanie-i-mekhanika.md`), не CLI-флагами станции.
#
# `aifa-calc`/`calc_run.py` (прямой legacy-потребитель, миграция которого —
# мандат Среза 13.5.2, не этого) свою половину (`own_calculated`) всё ещё
# несёт и не тронут; звено 2/3 этого файла (`_own_definitions_for` читает
# конверт готового прогона) продолжают работать НАД ЛЮБЫМ конвертом с этими
# ключами, включая старый calc-путь, — их тесты ниже не удалены и зелёные.
#
# РЕАЛЬНЫЙ ПРОБЕЛ, НЕ РЕШЁННЫЙ МОЛЧА: переживает ли теперь "своё" человека
# ход к ходу через `EXTRACT_AD_HOC_METRIC`/`CALCULATE_AD_HOC_METRIC` — то есть
# есть ли у `Case`/`CaseRuntime` метод «дай все ad-hoc спеки документа для
# превью следующему ходу» — этой сессией НЕ проверено (см. явную находку
# `23f-1-srez-13-5-4-station-listener-design.md`, группа C). Решение
# владельца нужно: это либо небольшой новый читающий метод у существующего
# `Case`/`SqlCaseStore` (не второе хранилище — расширение того же), либо
# сознательный отказ от старой гарантии «поправь хинт на втором шаге» до
# отдельной сессии. Зафиксировано в `Nedodelano.md`.


def test_the_next_turn_sees_them(monkeypatch):
    """Звено 3: модель получает СВОЮ ЖЕ запись, чтобы править её, а не сочинять."""
    _store(monkeypatch, {
        "own_metrics": [{"enum": "SHARES_OUTSTANDING", "name_ru": "Количество акций",
                         "hint": "Примечание про прибыль на акцию"}],
        "own_calculated": [{"enum": "ROCE_OWN", "name_ru": "ROCE",
                            "formula": "OPERATING_PROFIT / (TOTAL_ASSETS - X)"}],
    })
    блок = _own_definitions_for(["doc-1"], 7)
    assert "SHARES_OUTSTANDING" in блок and "ROCE_OWN" in блок
    assert "OPERATING_PROFIT / (TOTAL_ASSETS - X)" in блок, (
        "формула показана без тела — поправить в ней одно поле невозможно")
    assert "Примечание про прибыль на акцию" in блок, "потеряна подсказка поиска"
    assert "меняй" in блок or "правк" in блок.lower(), (
        "не сказано, что запись нужно ПРАВИТЬ, а не сочинять заново — ради "
        "этого всё и делалось")


def test_nothing_to_show_is_silence_not_an_empty_block(monkeypatch):
    """Самый частый случай: своих определений не было — и блока нет вовсе."""
    _store(monkeypatch, {})
    assert _own_definitions_for(["doc-1"], 7) == ""
    _store(monkeypatch, None)
    assert _own_definitions_for(["doc-1"], 7) == ""
    assert _own_definitions_for([], 7) == ""
    assert _own_definitions_for(None, 7) == ""


def test_a_broken_store_does_not_break_the_turn(monkeypatch):
    """Хранилище исходов может быть выключено — ход это переживает.

    На стенде без `FINENGINE_RUNS_DATABASE_URL` оно молчит всегда. Ронять ход
    из-за отсутствующей ПОДСКАЗКИ — значит менять работу на удобство.
    """
    def _boom():
        raise RuntimeError("хранилище исходов выключено")

    monkeypatch.setattr("finengine.store.runs.get_run_store", _boom)
    assert _own_definitions_for(["doc-1"], 7) == ""


def test_the_block_does_not_grow_without_bound(monkeypatch):
    """Документов в проекте бывает много — подсказка не должна съесть промпт."""
    _store(monkeypatch, {"own_calculated": [
        {"enum": f"OWN_{i}", "name_ru": "x", "formula": "A / B"} for i in range(3)]})
    блок = _own_definitions_for([f"doc-{i}" for i in range(50)], 7)
    assert len(блок.splitlines()) <= 12, (
        f"блок вырос до {len(блок.splitlines())} строк — он поедет в КАЖДЫЙ ход")
    источник = (КОРЕНЬ / "src" / "finengine" / "orchestrator"
                / "agent.py").read_text(encoding="utf-8")
    assert re.search(r"list\(doc_ids\)\[:\d+\]", источник), (
        "нет потолка на число документов — на большом проекте подсказка станет "
        "дороже самого вопроса")
