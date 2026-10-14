"""Строка `arch` — контракт между фронтом и бэком, и он держался на честном слове.

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. Гейтвей-петля хода живёт в бэкенде с #98 (26 июля):
`api/main.py` гейтит её сравнением `request.arch == "aifa_gateway"`. Но значения
`aifa_gateway` во фронте НЕ БЫЛО ВООБЩЕ — ни в union `ExtractionArch`, ни в одном
селекторе. То есть целая ветка исполнения существовала, была протестирована и
задеплоена, а попасть в неё пользователь не мог никак: только переменной
окружения на весь контейнер, то есть для ВСЕХ сразу.

Обнаружилось это через полторы недели и только потому, что понадобилось на ту
ветку встать. Ни один тест не покраснел: бэковые тесты гоняли `arch` напрямую,
фронтовые не знали про бэковые литералы.

Здесь проверяется ровно одна вещь: **каждый литерал `arch`, по которому бэкенд
принимает решение, фронт умеет прислать**. Обратное не проверяется намеренно —
фронт вправе знать значения, которые бэкенд трактует дефолтом (для них ветки в
коде нет по построению, см. `main.py` — «arch in (... "" / unset / unknown)»).

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `aifa_gateway` из union в `web/src/lib/prefs.ts`
— тест обязан покраснеть.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ (ревизия 21.08). Он текстовый по природе предмета:
инвариант — совпадение ЛИТЕРАЛА в двух местах, и вторая половина написана на
TypeScript, из pytest её не исполнить. Поэтому здесь проверяется только, что
значение ЕСТЬ в обоих файлах. Ни того, что фронт действительно шлёт его в
`/chat/turns`, ни того, что бэкенд по нему сворачивает в гейтвей-петлю, ни
работоспособности самой ветки он не проверяет: селектор может не отдавать это
значение наружу, а ветка — падать на первом же шаге, и тест останется зелёным.

🔴 РЕВИЗИЯ 21.08: снят `test_sources_exist` (2 параметра). Он проверял, что оба
файла существуют. Мутация (`mv web/src/lib/prefs.ts /tmp/`) роняет ОБА
содержательных теста ниже с `FileNotFoundError`, и путь назван прямо в
трассировке — отдельного теста для этого не нужно.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = ROOT / "src" / "finengine" / "api" / "main.py"
PREFS_TS = ROOT.parent / "web" / "src" / "lib" / "prefs.ts"

# Литералы, которые бэкенд сравнивает с arch, но которые фронт слать НЕ обязан.
# "aifa" — историческое написание, оставлено в бэке для старых сохранённых
# диалогов; заводить его во фронте заново незачем.
# "finengine_rag" — путь СНЯТ 13.08.2026, ветка осталась только чтобы ОТКАЗАТЬ
# старому клиенту с названной причиной (400). Она и должна быть недостижима из
# UI: это не исполнение, а отказ. Что отказ остался громким, стережёт
# tests/test_rag_path_stays_removed.py.
# "aifa_baseline" / "aifa_dev" — движки СНЯТЫ 21.08.2026 (легаси: считали по
# пятой копии реестра и после перевода доходностей в проценты отдавали числа
# в сто раз меньше). Ветка осталась ровно затем же, зачем у RAG: ОТКАЗАТЬ
# старому клиенту названной причиной. Сохранённый выбор уводит на петлю ядра
# `loadPrefs` (web/src/lib/prefs.ts), сторож перевода — prefs-arch-default.test.ts.
LEGACY_BACKEND_ONLY = {"aifa", "finengine_rag", "aifa_baseline", "aifa_dev"}


def _backend_arch_literals() -> set[str]:
    """Все строки, с которыми `main.py` сравнивает arch."""
    src = MAIN_PY.read_text(encoding="utf-8")
    found: set[str] = set()
    # arch == "x" / request.arch == "x"
    found |= set(re.findall(r'\barch\s*==\s*"([a-z_]+)"', src))
    # arch in ("x", "y") — забираем всё содержимое скобок
    for group in re.findall(r'\barch\s+in\s+\(([^)]*)\)', src):
        found |= set(re.findall(r'"([a-z_]+)"', group))
    return found - LEGACY_BACKEND_ONLY


def _frontend_arch_union() -> set[str]:
    """Значения union-типа ExtractionArch во фронте."""
    src = PREFS_TS.read_text(encoding="utf-8")
    m = re.search(r'export type ExtractionArch\s*=(.*?);', src, re.S)
    assert m, "не нашёл объявление ExtractionArch в prefs.ts — тест устарел"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def test_backend_arch_literals_are_reachable_from_ui() -> None:
    backend = _backend_arch_literals()
    frontend = _frontend_arch_union()
    assert backend, "не распознал ни одного литерала arch в main.py — тест устарел"
    missing = sorted(backend - frontend)
    assert not missing, (
        f"бэкенд принимает решение по arch={missing}, но фронт такое значение "
        f"прислать не может: во фронте {sorted(frontend)}. Ветка исполнения "
        f"недостижима из UI."
    )


def test_gateway_loop_is_reachable_from_ui() -> None:
    """Адресно про гейтвей-петлю — это шаг Ш0 миграции, а не общий случай."""
    assert "aifa_gateway" in _backend_arch_literals(), (
        "гейт гейтвей-петли в main.py пропал или переименован"
    )
    assert "aifa_gateway" in _frontend_arch_union(), (
        "aifa_gateway нет в ExtractionArch — на петлю гейтвея не встать из UI"
    )
