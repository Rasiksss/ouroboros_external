"""Комната живёт дольше, чем мы обещаем модели.

🔴 ЖИВОЙ ПРОГОН ВЛАДЕЛЬЦА 23.08.2026, НОНС 074ba292. Тело навыка обещает:
«КОМАНДА СИНХРОННАЯ, ИДЁТ 6–25 МИНУТ» — и модель по этому обещанию ждёт. А
сервис станции убивал `exec` на ПЯТНАДЦАТОЙ минуте (`EXEC_TIMEOUT_SEC=900`).

Извлечение 23 показателей с сегментной стадией в этот зазор и попало: запуск в
19:53:16, а к 20:18 комната отвечала

    exec http 409: action 'exec' is not allowed for status 'terminated'

Терминала не пришло НИКОГДА. Прогон записан упавшим при сделанной работе,
человек получил таблицу и молчание.

⚠️ ДЕФЕКТ — НЕ В ВЕЛИЧИНЕ, А В РАСХОЖДЕНИИ. Два наших собственных числа
противоречили друг другу: обещание в теле навыка и потолок в конфиге. Ни одно
из них не было «неправильным» само по себе — неправильно было то, что их никто
не держал вместе. Этот файл и держит.

ТРИ ЧИСЛА, КОТОРЫЕ ОБЯЗАНЫ СОГЛАСОВЫВАТЬСЯ:
  * обещание навыка (верхняя граница «6–25 минут»);
  * `timeoutSec`, который просит мост (`workstation-ext/bridge.mjs`);
  * `EXEC_TIMEOUT_SEC` — жёсткий потолок сервиса (`deploy/dev-station/run.sh`).
Мост не может просить больше потолка: сервис отобьёт «exceeds service hard cap».

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 23.08.2026, каждая покраснела):
  · вернуть `EXEC_TIMEOUT_SEC=900` → падает `test_the_service_cap_covers_the_promise`;
  · вернуть мосту `900` → падает `test_the_bridge_asks_for_what_the_skill_promises`;
  · попросить мосту БОЛЬШЕ потолка → падает `test_the_bridge_never_asks_above_the_cap`.
"""
from __future__ import annotations

import re
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[1]
НАВЫК = КОРЕНЬ / "station-skills" / "aifa-extract" / "SKILL.md"
МОСТ = КОРЕНЬ / "deploy" / "workstation-ext" / "bridge.mjs"
СТАНЦИЯ = КОРЕНЬ / "deploy" / "dev-station" / "run.sh"


def _обещание_минут() -> int:
    """Верхняя граница из тела навыка: «ИДЁТ 6–25 МИНУТ»."""
    m = re.search(r"ИДЁТ\s+(\d+)\s*[–-]\s*(\d+)\s+МИНУТ", НАВЫК.read_text(encoding="utf-8"))
    assert m, "в теле навыка больше нет обещания «идёт N–M минут» — сверять не с чем"
    return int(m.group(2))


def _мост_секунд() -> int:
    m = re.search(r'WS_EXEC_TIMEOUT_SEC \|\| "(\d+)"', МОСТ.read_text(encoding="utf-8"))
    assert m, "мост больше не задаёт timeoutSec по умолчанию"
    return int(m.group(1))


def _потолок_секунд() -> int:
    m = re.search(r"EXEC_TIMEOUT_SEC=(\d+)", СТАНЦИЯ.read_text(encoding="utf-8"))
    assert m, "у сервиса станции больше нет EXEC_TIMEOUT_SEC"
    return int(m.group(1))


def test_the_service_cap_covers_the_promise():
    обещано, потолок = _обещание_минут() * 60, _потолок_секунд()
    assert потолок >= обещано, (
        f"сервис убивает exec через {потолок} с, а навык обещает модели до "
        f"{обещано} с — прогон умрёт молча, как 074ba292 (23.08)")


def test_the_bridge_asks_for_what_the_skill_promises():
    обещано, мост = _обещание_минут() * 60, _мост_секунд()
    assert мост >= обещано, (
        f"мост просит {мост} с при обещанных {обещано} с — комната прервёт "
        "работу раньше, чем модель перестанет ждать")


def test_the_bridge_never_asks_above_the_cap():
    """Сервис отобьёт запрос выше потолка как «exceeds service hard cap»."""
    мост, потолок = _мост_секунд(), _потолок_секунд()
    assert мост <= потолок, (
        f"мост просит {мост} с при потолке сервиса {потолок} с — сервис ответит "
        "400, и ВСЕ exec агента упадут тихим exit 1 (грабля #84)")


def test_the_two_copies_of_the_bridge_agree():
    """Мост лежит двумя копиями; разъедутся — гейтвей возьмёт не ту."""
    вторая = КОРЕНЬ / "workstation-ext" / "bridge.mjs"
    if not вторая.is_file():
        return
    assert вторая.read_bytes() == МОСТ.read_bytes(), (
        "копии `bridge.mjs` разошлись — на гейтвей уедет одна, а править будут "
        "другую")
