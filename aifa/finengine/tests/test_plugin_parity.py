"""Плагин гейтвея и реестр тулов разъезжаются молча — и уже разъехались.

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. `deploy/gateway-plugins/fe-tools/index.ts`
генерируется из бэкового реестра, но генератор — РУЧНОЙ шаг. 26 июля плагин
собрали, 1 августа завели `fe_calc` в whitelist (#122: «арифметика в уме
запрещена»), генератор никто не перезапустил. Файл на стенде и файл в
репозитории совпадали ПОБАЙТНО (sha256 сходился), деплой был честный — и
ровно поэтому никто ничего не заподозрил. А в гейтвей-режиме модель просто не
видела калькулятор: `enable_tools = False`, тулы приходят только из плагина.

Разрыв был бы виден на ходу «посчитай коэффициент», но такой ход в
гейтвей-режиме за полторы недели никто не сделал.

Формула паритета НЕ равенство «реестр ∩ whitelist == contracts.tools» — оно
красно по построению: `ask_user` есть в whitelist, но намеренно исключён из
плагина, а `fe_ping` есть в плагине, но не заводился в реестре. Поэтому:

    contracts.tools == (реестр ∩ whitelist − EXCLUDE) ∪ EXPLICIT_EXTRA

и оба множества импортируются ИЗ САМОГО ГЕНЕРАТОРА — иначе список исключений
станет третьим источником истины и разъедется со своими двумя.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: удалить любое имя из `contracts.tools` в
`openclaw.plugin.json` — тест обязан покраснеть.

⚠️ ЧЕГО ЭТОТ ФАЙЛ НЕ ЛОВИТ (ревизия 21.08.2026). Он весь — ПАРИТЕТ ДВУХ МЕСТ и
ЧТЕНИЕ ТЕКСТА СКРИПТА, и исполнить его нечем: половина инварианта живёт в
TypeScript плагина, а вторая — в шелле выкатки, который ходит по `ssh` на стенд
и запускаться в тесте не может. Отсюда три слепые зоны, которые нельзя принять
за проверку поведения:
  · **совпало ≠ работает.** Файлы плагина могут побайтно сойтись с реестром, а
    гейтвей — не загрузить плагин вовсе (так и было 26.07–01.08: sha256
    сходился, деплой был честный, калькулятора у модели не было);
  · **строка ≠ исполнение.** Про `ship.sh` доказуемо только то, что нужные
    строки стоят в нужных ветках. Что ветка исполняется на живой выкатке,
    текстом не показать — сторож `test_the_deployment_calls_the_plugin_deliverer`
    прямо говорит об этом в своей докстроке;
  · **описание ≠ поведение модели.** Сверка `description`/`parameters`
    доказывает, что схема доехала, и НИЧЕГО не говорит о том, позовёт ли модель
    тул и правильно ли его поймёт.
Снят `test_sources_exist` (три параметра): его предмет полностью покрыт
соседями — проверено 21.08 прятанием каждого из трёх файлов по очереди, каждый
раз краснели один-два содержательных теста ВМЕСТЕ с ним.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "deploy" / "gateway-plugins" / "fe-tools"
MANIFEST = PLUGIN_DIR / "openclaw.plugin.json"
INDEX_TS = PLUGIN_DIR / "index.ts"
WHITELIST = ROOT / "deploy" / "tools_whitelist.yaml"

sys.path.insert(0, str(ROOT / "deploy" / "gateway-plugins"))
os.environ.setdefault("FINENGINE_DATA_DIR", "/tmp/fe-tools-gen")
import gen_fe_tools as gen  # noqa: E402


def _registry_names() -> set[str]:
    from finengine.llm import tools as T

    return {d["name"] for d in T.all_definitions() if d.get("name")}


def _whitelist_names() -> set[str]:
    data = yaml.safe_load(WHITELIST.read_text(encoding="utf-8"))
    return set(data.get("enabled") or [])


def _manifest_names() -> set[str]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return set(data.get("contracts", {}).get("tools") or [])


def _expected_names() -> set[str]:
    return ((_registry_names() & _whitelist_names()) - gen.EXCLUDE) | set(gen.EXPLICIT_EXTRA)


def _index_ts_definitions() -> dict[str, dict]:
    import re

    src = INDEX_TS.read_text(encoding="utf-8")
    m = re.search(r"const DEFINITIONS[^=]*=\s*(\[.*?\n\]);", src, re.S)
    assert m, "не нашёл массив DEFINITIONS в index.ts — тест устарел"
    return {d["name"]: d for d in json.loads(m.group(1))}


def _expected_definitions() -> dict[str, dict]:
    """Ровно то, что положил бы генератор: описание реестра, срезанное по
    DESC_LIMIT, и схема параметров как есть; плюс добавки вне реестра."""
    from finengine.llm import tools as T

    out = {
        d["name"]: {
            "description": (d.get("description") or "")[: gen.DESC_LIMIT],
            "parameters": d.get("parameters") or {"type": "object", "properties": {}},
        }
        for d in T.all_definitions()
        if d.get("name") and d["name"] not in gen.EXCLUDE
    }
    for name, extra in gen.EXPLICIT_EXTRA.items():
        out[name] = {
            "description": extra["description"],
            "parameters": extra["parameters"],
        }
    return out


def test_plugin_matches_registry_and_whitelist() -> None:
    expected, actual = _expected_names(), _manifest_names()
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    assert not missing, (
        f"тулы есть в реестре и разрешены, но в плагин НЕ попали: {missing}. "
        f"В гейтвей-режиме модель их не увидит. Перегенерируй: "
        f"PYTHONPATH=src .venv/bin/python deploy/gateway-plugins/gen_fe_tools.py"
    )
    assert not extra, (
        f"в плагине есть тулы, которых нет ни в (реестр ∩ whitelist), ни в "
        f"EXPLICIT_EXTRA: {extra}. У модели инструмент вне каталога одобрения."
    )


def test_index_ts_matches_manifest() -> None:
    """index.ts и манифест — два выхода одного генератора; расходиться не вправе."""
    import re

    src = INDEX_TS.read_text(encoding="utf-8")
    m = re.search(r"const DEFINITIONS[^=]*=\s*(\[.*?\n\]);", src, re.S)
    assert m, "не нашёл массив DEFINITIONS в index.ts — тест устарел"
    in_ts = {d["name"] for d in json.loads(m.group(1))}
    assert in_ts == _manifest_names(), (
        f"index.ts и openclaw.plugin.json разошлись: только в ts {sorted(in_ts - _manifest_names())}, "
        f"только в манифесте {sorted(_manifest_names() - in_ts)}. Перегенерируй оба."
    )


def test_plugin_carries_current_descriptions_and_schemas() -> None:
    """Сторож имён СЛЕП к дрейфу описаний — и разрыв дожил до прода.

    17.08 в реестре у `fe_scenario_settings` появились ручки `event_groups`
    (какие типы событий в календаре) и `macro_mode` (какой блок экономики в
    выпуске); обе реализованы на бэке. Плагин не перегенерировали — множества
    имён совпадали, тест был зелёный, а в гейтвей-режиме схемы приходят модели
    ТОЛЬКО из плагина: рабочая возможность просто исчезла из инструмента.
    Поэтому сверяем не имена, а (description, parameters) против реестра.

    ПРОВЕРКА СТОРОЖА ОТКАТОМ: испортить любое описание в index.ts — красный.
    """
    expected, actual = _expected_definitions(), _index_ts_definitions()
    drift: list[str] = []
    for name, exp in expected.items():
        got = actual.get(name)
        # Отсутствующие имена — забота test_plugin_matches_registry_and_whitelist;
        # здесь только содержимое, иначе одна поломка кричит из двух тестов.
        if got is None:
            continue
        if got.get("description") != exp["description"]:
            drift.append(f"{name}: описание")
        if got.get("parameters") != exp["parameters"]:
            drift.append(f"{name}: схема параметров")
    assert not drift, (
        f"плагин везёт УСТАРЕВШИЕ схемы: {drift}. В гейтвей-режиме модель видит "
        f"только их — новые ручки бэка ей недоступны. Перегенерируй: "
        f"PYTHONPATH=src .venv/bin/python deploy/gateway-plugins/gen_fe_tools.py"
    )


def test_fe_calc_reached_the_plugin() -> None:
    """Адресно про калькулятор: из-за него и завёлся этот файл (#122/#137).

    Проверка привязана к РЕЕСТРУ, а не к имени. Прежняя редакция требовала
    `fe_calc` безусловно и краснела на прод-ветке, где такого тула нет вовсе:
    сторож объявлял дефектом чужой состав каталога и блокировал доставку
    плагина на прод (поймано 18.08 на первом же запуске ship-plugin.sh).
    """
    from finengine.llm import tools as T

    if "fe_calc" not in {d.get("name") for d in T.all_definitions()}:
        pytest.skip("на этой ветке fe_calc нет в реестре — проверять нечего")
    assert "fe_calc" in _manifest_names(), (
        "fe_calc нет в плагине — в гейтвей-режиме модель снова считает в уме"
    )


def test_no_description_silently_truncated() -> None:
    """Обрезка описания = модель читает инструкцию, оборванную на полуслове."""
    from finengine.llm import tools as T

    too_long = {
        d["name"]: len(d.get("description") or "")
        for d in T.all_definitions()
        if d.get("name") and d["name"] not in gen.EXCLUDE
        and len(d.get("description") or "") > gen.DESC_LIMIT
    }
    assert not too_long, (
        f"описания длиннее потолка {gen.DESC_LIMIT} и будут обрезаны: {too_long}. "
        f"Сократи описание или подними DESC_LIMIT осознанно."
    )


# ── Плагин доезжает до гейтвея, а не только до репозитория ──────────────────

SHIP = ROOT / "deploy" / "skill-hub" / "ship.sh"
DELIVERER = ROOT / "deploy" / "dev-gateway" / "ship-plugin.sh"


def _code_lines(path: Path) -> list[str]:
    """Строки скрипта БЕЗ комментариев.

    ⚠️ Сторож, ищущий имя по всему тексту, зеленеет от объяснительной записки:
    имя доставщика встречается и в шапке, и в `echo`. Здесь это не теория —
    рядом в этом же блоке ship.sh стоит `echo "запускаю доставщик: …"`.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        head = line.split("#", 1)[0]
        if head.strip():
            out.append(head)
    return out


def test_the_deployment_calls_the_plugin_deliverer() -> None:
    """Выкатка ЗАПУСКАЕТ доставщик плагина, а не упоминает его.

    ЦЕНА ЭТОГО СТОРОЖА (19.08.2026). `deploy/dev-gateway/ship-plugin.sh` умеет
    всё: перегенерировать, проверить паритет, положить файлы, синхронизировать
    alsoAllow, перезапустить гейтвей. И `grep -rn ship-plugin` находил его имя
    ТОЛЬКО в его собственной шапке — ни один путь выкатки его не звал. В тот
    день `fe_registry_find` сняли в репозитории, а модель на стенде продолжала
    его звать: в гейтвей-режиме схемы приходят ей ТОЛЬКО из плагина.

    Проверка сторожа откатом: убрать строку `bash …/ship-plugin.sh` из
    ship.sh — красное; подменить её на `echo` — тоже красное.
    """
    assert DELIVERER.is_file(), f"доставщик плагина пропал: {DELIVERER}"
    ship = "\n".join(_code_lines(SHIP))
    runs = [ln for ln in _code_lines(SHIP)
            if "dev-gateway/ship-plugin.sh" in ln and ln.strip().startswith("bash ")]
    assert runs, (
        "выкатка не запускает deploy/dev-gateway/ship-plugin.sh — плагин "
        "останется в репозитории, а модель на стенде продолжит видеть "
        "вчерашний набор тулов (упоминания в комментарии и в echo не считаются)")

    # Строка на месте — ещё не значит, что она исполняется: подмена условия на
    # `if false; then` оставляла сторож зелёным при мёртвой доставке. Полной
    # достижимости текстом не докажешь, но условие обязано читать FE_DRIFT —
    # переменную, которую выставляет сверка md5 выше.
    branch = re.search(r'if \[ "\$FE_DRIFT" = 1 \]; then(.*?)\n  fi\b', ship, re.S)
    assert branch and "dev-gateway/ship-plugin.sh" in branch.group(1), (
        "запуск доставщика стоит вне ветки по $FE_DRIFT — условие подменили, и "
        "доставка мертва при живой строке")


def test_the_deployment_compares_every_file_the_deliverer_ships() -> None:
    """Сверка дрейфа охватывает ВСЕ файлы, которые доставщик кладёт на гейтвей.

    ЦЕНА. `ship.sh` считал md5 по двум файлам, а `ship-plugin.sh` (шаг 3/5)
    везёт три: расхождение по `package.json` доставку не запускало. А именно
    этот файл объявляет ядру точку входа плагина — разойдись он, и гейтвей
    возьмёт вчерашний `index.ts` при совпавших md5 остальных двух.

    Список берётся ИЗ доставщика, а не переписывается сюда: иначе он станет
    третьим источником истины и разъедется со своими двумя.

    Проверка сторожа откатом: убрать любое имя из цикла `for f in …` в ship.sh
    — красное; добавить четвёртый файл в scp доставщика и забыть про ship.sh —
    тоже красное.
    """
    ship = "\n".join(_code_lines(SHIP))
    shipped = set(re.findall(r'fe-tools/([A-Za-z0-9_.-]+)"',
                             "\n".join(_code_lines(DELIVERER))))
    assert len(shipped) >= 3, (
        f"в доставщике разобралось {len(shipped)} файлов ({sorted(shipped)}) — "
        f"читаем не то, сторож стал бы вакуумным")

    loop = re.search(r"for f in ([^;]+); do", ship[ship.index("сверка плагина fe-tools"):])
    assert loop, "в ship.sh пропал цикл сверки файлов плагина fe-tools"
    compared = set(loop.group(1).split())
    missing = sorted(shipped - compared)
    assert not missing, (
        f"доставщик везёт на гейтвей {missing}, а сверка дрейфа их не смотрит: "
        f"расхождение по ним доставку не запустит")


def test_the_deployment_refuses_to_ship_a_plugin_that_is_not_in_git() -> None:
    """Выкатка ОТКАЗЫВАЕТ, если файлы плагина расходятся с git.

    ЦЕНА. Бэкенд на VPS собирается из `git pull`, а доставщик плагина берёт
    файлы из ЛОКАЛЬНОГО рабочего дерева — два источника кода в одной выкатке.
    Плюс шаг 1/5 доставщика перегенерирует плагин прямо в рабочем дереве, и с
    тех пор как его зовёт `ship.sh`, это побочный эффект ОБЫЧНОЙ выкатки.
    Незакоммиченное содержимое уехало бы на стенд молча — состояние, которого
    нет ни в одной ветке, и следующий `git pull` его не привезёт.

    Отказ нужен ДВАЖДЫ: до доставщика (уже лежащие правки) и после него
    (правки, которые сделала перегенерация).

    Проверка сторожа откатом: убрать любую из двух проверок — красное.
    """
    ship = "\n".join(_code_lines(SHIP))
    assert re.search(r"status --porcelain\b[^\n]*gateway-plugins/fe-tools", ship), (
        "выкатка не сверяет файлы плагина с git — на стенд уедет локальное "
        "рабочее дерево, а бэкенд соберётся из git pull")

    call = ship.index("dev-gateway/ship-plugin.sh")
    uses = [m.start() for m in re.finditer(r"\$\(fe_plugin_vs_git\)", ship)]
    before = [u for u in uses if u < call]
    after = [u for u in uses if u > call]
    assert before, (
        "перед запуском доставщика нет отказа по расхождению с git — "
        "незакоммиченный плагин уедет на стенд")
    assert after, (
        "после доставщика нет отказа по расхождению с git — перегенерация "
        "шага 1/5 останется незакоммиченной, а на стенде будет то, чего нет в git")
    for use in before + after:
        assert "exit 1" in ship[use:use + 600], (
            "расхождение с git найдено, но выкатка продолжается — "
            "предупреждение без отказа не сторож")
