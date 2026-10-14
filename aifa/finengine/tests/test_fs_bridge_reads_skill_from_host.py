"""Тело навыка читается с диска гейтвея, а не шеллом в комнату станции (#222).

ЗАЧЕМ ЭТОТ ФАЙЛ. Ядро объявляет модели тело навыка путём
`<remoteWorkspaceDir>/.openclaw/sandbox-skills/skills/<навык>/SKILL.md` и
материализует навыки КОПИЕЙ на диске гейтвея — этот путь приходит мосту полем
`localRoot` у монтирований `source === "protectedSkill"`. Штатный мост читает
файл шеллом В КОМНАТЕ станции, где этих файлов нет и быть не должно: комната
запечатана, полка приезжает туда архивом. Вдобавок `stat` штатного моста зовёт
`readlink -f` и `stat -c`, а busybox комнаты собран на десять апплетов — их там
нет вовсе.

Замер 12.08.2026 (сессия fce9cba6-…): два `read` → `Sandbox FS error (ENOENT)`,
девять `exec` вслепую, тело добыто ТРИНАДЦАТЫМ вызовом через
`python3 -c "zipfile…"` по `.shelf.zip`. Шестнадцать секунд, чтобы прочитать
один файл, — и только потому, что модель догадалась. На боевом ходе анализа она
догадываться не станет: витрина говорит, что делать, и она зовёт `fe_analyze`
сразу. То есть ПОРЯДОК РАБОТЫ из тела навыка не применялся вообще.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ — по тексту настоящего `deploy/workstation-ext/index.ts`:
  1. `stat` и `readFile` в `createFsBridge` переопределены;
  2. отбор путей идёт по `source === "protectedSkill"` через `getMounts`;
  3. для НЕ-навыков оба метода делегируют в оригинал (иначе сломаем всё
     остальное файловое хозяйство хода);
  4. `writeFile`/`mkdirp` по пути навыка ОТКАЗЫВАЮТ вслух — иначе запись уйдёт
     мимо читаемой копии и потеряется молча;
  5. `workdir` остался виртуальным `/workspace` — сторож от того, что следующий
     заход «заодно» переедет на реальный путь комнаты и вернёт регрессию `exec`
     плюс утечку `/tmp/ws/sessions/oc-agent-…` в приглашение модели.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ДОКАЗЫВАЕТ. Он текстовый. Мост живёт внутри процесса
гейтвея, позвать его из pytest нельзя. Работоспособность чтения подтверждает
ИСКЛЮЧИТЕЛЬНО живой ход: в трассе один успешный `read` по пути из витрины ДО
вызова `fe_analyze`, без единого `Sandbox FS error`. Зелёный тест при сломанном
чтении — возможное состояние, и это надо помнить.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать из `createFsBridge` строку `bridge.readFile =`
(оставив `bridge.stat =`) — тест 1 обязан покраснеть. Убрать отказ `read-only`
из `writeFile` — тест 4. Заменить литерал `const workdir = "/workspace"` на
любой другой — тест 5.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "deploy" / "workstation-ext" / "index.ts"


@pytest.fixture(scope="module")
def src() -> str:
    assert PLUGIN.is_file(), f"плагин не найден: {PLUGIN}"
    return PLUGIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bridge_body(src: str) -> str:
    """Тело createFsBridge — проверяем правку там, а не где-то ещё в файле."""
    start = src.find("createFsBridge(")
    assert start > 0, "createFsBridge в плагине не найден — правка #222 потеряна"
    return src[start:]


def test_stat_and_readfile_are_overridden(bridge_body: str) -> None:
    """1. Оба метода переопределены. Одного stat мало: read зовёт и то, и другое."""
    assert "bridge.stat =" in bridge_body, (
        "не переопределён stat — ядро зовёт его до чтения (assertSandboxFileExists) "
        "и получит ENOENT из комнаты станции"
    )
    assert "bridge.readFile =" in bridge_body, (
        "не переопределён readFile — stat пройдёт, а чтение уйдёт шеллом в комнату, "
        "где файла нет"
    )


def test_selects_paths_by_protected_skill_mount(bridge_body: str) -> None:
    """2. Отбор идёт по монтированию навыка, а не по строковому совпадению пути."""
    assert "getMounts()" in bridge_body, (
        "localRoot берётся не из getMounts — значит путь на диске гейтвея "
        "угадывается, а не спрашивается у ядра"
    )
    assert 'source === "protectedSkill"' in bridge_body, (
        "нет фильтра по source === \"protectedSkill\" — латка накроет чужие "
        "монтирования (workspace, agent) и сломает обычные файловые операции"
    )
    assert "localRoot" in bridge_body and "containerRoot" in bridge_body


def test_non_skill_paths_delegate_to_original(bridge_body: str) -> None:
    """3. Всё, что не навык, идёт в оригинальный мост — байт в байт как раньше."""
    for orig in ("origStat(params)", "origReadFile(params)"):
        assert orig in bridge_body, (
            f"нет делегирования {orig} — для не-навыков поведение изменится, "
            "а менять его мы не собирались"
        )


def test_write_into_skill_is_refused_loudly(bridge_body: str) -> None:
    """4. Запись в каталог навыка отказывает вслух.

    Штатная проверка записи спрашивает КОМНАТУ, где каталога нет, и потому
    пропускает запись — та ушла бы мимо читаемой копии и пропала бы молча.
    """
    for method in ("bridge.writeFile =", "bridge.mkdirp ="):
        assert method in bridge_body, f"не закрыт {method} — немая потеря правки"
    assert bridge_body.count("read-only") >= 2, (
        "отказ по записи в путь навыка не назван словами"
    )


def test_workdir_stays_virtual(src: str) -> None:
    """5. Переезд workdir на реальный путь комнаты НЕ состоялся и не должен.

    Он не нужен: getMounts резолвит /workspace/.openclaw/sandbox-skills/skills
    и при виртуальном workdir. А переезд ломает работающий exec и утаскивает
    /tmp/ws/sessions/oc-agent-… в приглашение модели, где путь меняется от
    сессии к сессии и рвёт префикс-кэш витрины.
    """
    assert re.search(r'const\s+workdir\s*=\s*"/workspace"', src), (
        "workdir больше не /workspace — кто-то переехал на реальный путь комнаты; "
        "см. раздел «чего не трогаем» в задаче #222"
    )


def test_kill_switch_exists(src: str) -> None:
    """Выключатель на откат без выкатки кода — обязателен для латки чужого класса."""
    assert "AIFA_SKILL_HOST_READ" in src, (
        "нет выключателя AIFA_SKILL_HOST_READ — откат потребует scp и git revert"
    )
    assert "SKILL_HOST_READ" in src and "if (!SKILL_HOST_READ) return bridge;" in src, (
        "выключатель объявлен, но не выключает: ранний возврат оригинального моста "
        "не найден"
    )
