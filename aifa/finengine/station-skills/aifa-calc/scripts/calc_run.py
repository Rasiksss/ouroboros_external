#!/usr/bin/env python3
"""AIFA CLAW — расчёт коэффициентов по таблице прогона (станция).

ЗАЧЕМ ОТДЕЛЬНЫМ НАВЫКОМ. Коэффициенты (долг/EBITDA, маржа, FCF, NIM, CoR) обязан
считать КОД по формулам реестра, а не модель в уме (#74). Пока расчёт был спрятан
внутри движка извлечения, отказ формулы никому не показывался: строка приезжала
пустой, и объяснить, почему, было нечем. Здесь отказ печатается словами и с
недостающим входом.

ЧИСЛА БЕРУТСЯ ТОЛЬКО ИЗ ФАЙЛА ПРОГОНА. Текст отчёта сюда не приходит вовсе —
это и есть гарантия, что посчитанное опирается на извлечённое, а не на второе
прочтение отчёта, которое разошлось бы с первым.

ОДИН ФАЙЛ — ОДНА ТАБЛИЦА. Коэффициенты дописываются в тот же конверт
`metrics.json`, а не рядом. Потребитель (`analyze.py`) читает из файла ровно ключ
`metrics`; отдельный `ratios.json` до анализа бы не доехал, и анализ снова считал
бы коэффициенты в уме — то есть вернулся бы ровно тот дефект, ради которого всё
затевалось.

ЭСТАФЕТА ТЕРМИНАЛА. Извлечение и расчёт делят ОДИН нонс-канал хода, а очередь
закрывается первым же `done` (`station_run_sink.drain`). Поэтому извлечение
зовётся с `--no-final`, а терминальное событие ставит этот скрипт по `--final` —
и ставит ВСЕГДА, даже когда считать было нечего: иначе дренаж досидел бы до
конца жизни воркера.

Запуск:
  python calc_run.py --metrics ./metrics.json [--only EBITDA,долг] [--final]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

EXIT_BAD_INPUT = 2
# Потолок строк «не посчитано» в stdout: у нефинансовой ячейки формул полсотни,
# и на неполной таблице отказ по каждой — простыня в контекст модели (#207).
# Причины по всем остаются в файле прогона.
_SKIPPED_LINES = 12
EXIT_BROKEN_REGISTRY = 3
EXIT_CALC_FAILED = 4


def _load_envelope(path: Path) -> dict:
    """Конверт прогона: {run, metrics, segments, group}. Кривой — отказ."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("metrics"), list):
        raise ValueError(
            f"{path}: это не конверт прогона — нет списка 'metrics'. "
            f"Файл пишет извлечение флагом --out-json")
    return data


def _write_atomically(path: Path, envelope: dict) -> None:
    """Запись через временный файл: оборванный прогон не оставит полтаблицы."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _missing_inputs(deps: list, available: set[str]) -> list[str]:
    """Входы формулы, которых нет в таблице прогона."""
    return [d for d in (str(x).strip().upper() for x in deps or ())
            if d and d not in available]


def available_inputs(rows: list, computed: list | None = None) -> set[str]:
    """Имена, которые СЧИТАЮТСЯ ДОБЫТЫМИ для причины отказа.

    Вход добыт, если по нему есть ЧИСЛО. Пустая строка входом не является:
    назвать её в причине — соврать («формула не дала значения» вместо «нет
    CAPEX»).

    🔴 ПОСЧИТАННОЕ — ТОЖЕ ДОБЫТОЕ (прогон владельца 61a6c634, 25.08.2026).
    Набор строился по одним ИЗВЛЕЧЁННЫМ строкам. Но вход формулы бывает
    расчётным: своя `FCF_PER_SHARE = FREE_CASH_FLOW / SHARES_OUTSTANDING`
    опирается на реестровый FREE_CASH_FLOW, а он в том же прогоне посчитался и
    имел значение −38 609. В причине отказа он всё равно оказался
    «недостающим».

    Ложная причина хуже расплывчатой: она посылает модель переизвлекать то, что
    уже лежит строкой выше, — прямо в петлю, от которой мы её отговариваем.

    `computed` — посчитанное ПРЯМО СЕЙЧАС: своя формула может опираться на
    реестровую из того же прохода.
    """
    добыто: set[str] = set()
    for r in [*(rows or ()), *(computed or ())]:
        if not isinstance(r, dict):
            continue
        есть_число = r.get("value_current") is not None
        if r.get("is_calculated"):
            подходит = есть_число
        else:
            подходит = bool(r.get("found")) or есть_число
        имя = str(r.get("enum") or "").strip().upper()
        if имя and подходит:
            добыто.add(имя)
    return добыто


def deps_map(calculated: list, own: list[dict]) -> dict[str, list[str]]:
    """Имя формулы → её входы. РЕЕСТРОВЫЕ И СВОИ ВМЕСТЕ.

    🔴 ЗАЧЕМ ОТДЕЛЬНОЙ ФУНКЦИЕЙ (прогон владельца a89c6a9f, 24.08.2026).
    Карта строилась одной строкой и ТОЛЬКО по реестру, а `calc_missing`
    считается по ней. Своя формула реестру неизвестна по устройству — значит её
    входы выходили пустым списком, и отказ приезжал БЕЗ ПРИЧИНЫ: строка
    `FCF_PER_SHARE` встала в таблицу пустой, `calc_missing: []`, и почему — не
    сказал никто.

    Владелец просил «доходность на акцию по свободному денежному потоку»
    ВСЛУХ. Пустая клетка без объяснения по прямой просьбе хуже отказа: по ней не
    понять, чего не хватило и что делать дальше. Не хватало SHARES_OUTSTANDING —
    модель написала `FCF / SHARES_OUTSTANDING`, но самого входа не заказала и
    рыночным не пометила.

    ⚠️ РЕЕСТР СИЛЬНЕЕ ПРИ СОВПАДЕНИИ ИМЁН. `own_calc_defs` такие имена уже
    отклоняет, но правило должно быть видно и здесь: иначе однажды его снимут
    там, и подмена определения пройдёт молча.
    """
    карта = {str(c[0]).strip().upper(): [str(d).strip().upper() for d in (c[3] or ())]
             for c in calculated or ()}
    for свой in own or ():
        имя = str(свой.get("enum") or "").strip().upper()
        if имя:
            карта.setdefault(имя, [str(d).strip().upper() for d in (свой.get("deps") or ())])
    return карта


def замкнуть_по_зависимостям(only: list[str], calculated: list) -> tuple[list[str], set[str]]:
    """`--only NET_DEBT` обязан посчитать и TOTAL_DEBT, иначе не посчитает ничего.

    🔴 ДЕФЕКТ, РАДИ КОТОРОГО ЭТО НАПИСАНО (найден прогоном по НОВАТЭКу 29.08.2026).
    Реестровый коэффициент может зависеть от ДРУГОГО рассчитываемого:

        NET_DEBT       = TOTAL_DEBT − CASH_EQUIVALENTS − BANK_DEPOSITS
        FREE_CASH_FLOW = NET_OPERATING_CASH_FLOW − CAPEX

    `--only` сужал состав расчёта буквально, поэтому `--only NET_DEBT` не считал
    `TOTAL_DEBT`, и чистый долг выходил ПУСТЫМ. Без сужения он считается — то
    есть «посчитай чистый долг» отдельной просьбой не работало, а хвостом
    прогона работало.

    Хуже пустоты был диагноз: «TOTAL_DEBT — не заказывали (нужно извлечение)».
    Извлечение рассчитываемый показатель не добудет НИКОГДА, и модель, послушав
    совет, уходила переизвлекать документ на 6–25 минут за тем же результатом.

    Возвращает (расширенный состав, что просили изначально) — второе нужно, чтобы
    добранные по дороге входы считались, но не лезли на экран как заказанные.
    """
    имена = {t[0] if isinstance(t, (list, tuple)) else str(t.get("enum") or "")
             for t in calculated}
    deps = {}
    for t in calculated:
        if isinstance(t, (list, tuple)) and len(t) >= 4:
            deps[t[0]] = list(t[3] or ())
        elif isinstance(t, dict) and t.get("enum"):
            deps[t["enum"]] = list(t.get("deps") or ())
    запрошено = {str(e).strip().upper() for e in only}
    состав, очередь = set(запрошено), list(запрошено)
    while очередь:
        имя = очередь.pop()
        for d in deps.get(имя, ()):
            d = str(d).strip().upper()
            # Добираем ТОЛЬКО рассчитываемые: извлекаемые входы приходят из
            # конверта, и «добрать» их здесь нечем.
            if d in имена and d not in состав:
                состав.add(d)
                очередь.append(d)
    return sorted(состав), запрошено


def _diagnose(missing: list[str], ordered: set[str], market: set[str]) -> list[str]:
    """Почему входа нет — с ДЕЙСТВИЕМ, а не просто именем.

    🔴 ЗАЧЕМ (вопрос владельца 24.08: «как модель поймёт, что нам не хватает
    чего-то для расчёта?»). Раньше отказ говорил «нет входов: A, B» — и три
    совершенно РАЗНЫХ случая выглядели одинаково, хотя требуют противоположных
    действий:

      * входа НЕ ЗАКАЗЫВАЛИ → нужно извлечение, он в отчёте может быть;
      * вход ЗАКАЗЫВАЛИ, движок искал и не нашёл → переизвлечение бесполезно,
        этой строки в документе нет: переписывай формулу или скажи «не считается»;
      * вход РЫНОЧНЫЙ (цена акции, курс, ставка) → в отчёте его нет по природе,
        нужен внешний источник.

    Модель различала их догадкой. А код различает ТОЧНО: первые два — по
    `scope.read` прогона, третий — по признаку `source: "market"` в СВОЕЙ записи
    показателя. Признак ставит модель, когда придумывает показатель: откуда он
    берётся, знает она, а не мы.
    """
    из_отчёта = []
    for имя in missing:
        if имя in market:
            из_отчёта.append(f"{имя} — рыночная величина, в отчёте её нет (нужен внешний источник)")
        elif имя in ordered:
            из_отчёта.append(f"{имя} — искали, в отчёте НЕТ (переизвлечение не поможет)")
        else:
            из_отчёта.append(f"{имя} — не заказывали (нужно извлечение)")
    return из_отчёта


# ── РАСЧЁТ КОДОМ (решение владельца 29.08.2026) ──────────────────────────────
#
# ЗАЧЕМ. Вычислитель формул умеет `+ − × /` и ABS/MIN/MAX/AVG/ANNUAL — этого
# хватает на коэффициенты, то есть на отношения и доли. Как только во времени
# появляется степень (дисконтирование, CAGR, сложный процент) или перебор
# (IRR, сценарии, чувствительность), выражением это не записать.
#
# Раньше такую просьбу закрывать было нечем, и модель считала в уме — число
# приходило текстом, мимо таблицы, без единицы и без возможности проверить.
#
# ЧЕМ ЭТО НЕ `eval` В БЭКЕНДЕ. Скрипт исполняется В ЗАПЕЧАТАННОЙ КОМНАТЕ: сети
# наружу нет, диск свой, живёт минуты. Комната для того и заведена, чтобы в ней
# шёл недоверенный код. Отказ ниже — страховка от того, что этот же файл
# однажды позовут из процесса бэкенда, где такой границы нет.
#
# БУХГАЛТЕРИЯ НА АВТОРЕ. Реестровой формуле движок сам выводит размерность,
# помнит входы и считает второй период. У скрипта этого нет ни у кого, поэтому
# `unit` требуется, а необъявленные допущения помечаются вслух: «EV = 2 366 076»
# без слова о ставке дисконтирования — это не результат, а число.
_SCRIPT_FIELDS = ("enum", "name_ru", "value_current", "unit")


def _run_script(path: str, metrics: list, run: dict, profile_id: str) -> list[dict]:
    """Исполнить скрипт модели в комнате и забрать объявленные им строки."""
    from finengine.llm.station_bridge import _rpc_dir  # noqa: PLC0415
    from finengine.skills.calc import CalcError  # noqa: PLC0415

    if not os.environ.get("FE_RPC_DIR") and not os.path.isdir(_rpc_dir()):
        raise CalcError(
            "расчёт скриптом работает ТОЛЬКО в комнате станции: признак комнаты "
            "(каталог моста) не найден. Вне её исполнять присланный код нельзя")

    src = Path(path)
    if not src.is_file():
        raise CalcError(f"нет файла скрипта '{path}'")

    # Скрипту подаём готовое: величины прогона, ячейку и блок run. Всё
    # остальное он берёт импортом — `finengine` лежит на полке, и выверенный
    # расчёт зовётся оттуда, а не переписывается заново.
    ns: dict = {"metrics": metrics, "run": run, "cell": profile_id,
                "__name__": "__aifa_script__", "__file__": str(src)}
    try:
        exec(compile(src.read_text(encoding="utf-8"), str(src), "exec"), ns)  # noqa: S102
    except Exception as exc:  # noqa: BLE001 — текст ошибки уходит модели, она правит и повторяет
        raise CalcError(f"скрипт упал: {type(exc).__name__}: {exc}") from exc

    сырьё = ns.get("RESULT") or ns.get("РЕЗУЛЬТАТ") or []
    if not isinstance(сырьё, list):
        raise CalcError("RESULT должен быть списком записей, получено "
                        f"{type(сырьё).__name__}")

    готовые: list[dict] = []
    for i, зап in enumerate(сырьё):
        if not isinstance(зап, dict):
            raise CalcError(f"RESULT[{i}] не запись, а {type(зап).__name__}")
        нет = [f for f in _SCRIPT_FIELDS if not str(зап.get(f) or "").strip()
               and зап.get(f) is None]
        if нет:
            raise CalcError(f"RESULT[{i}]: не заполнено {нет}. Единица обязательна: "
                            "число без размерности в таблице неотличимо от денег")
        try:
            значение = float(зап["value_current"])
        except (TypeError, ValueError):
            raise CalcError(f"RESULT[{i}]: value_current не число") from None
        строка = {
            "type": "metric",
            "enum": str(зап["enum"]).strip().upper(),
            "name_ru": str(зап["name_ru"]).strip(),
            "value_current": значение,
            "value_prev": зап.get("value_prev"),
            "unit": str(зап["unit"]).strip(),
            "found": True,
            "is_calculated": True,
            # Чем посчитано — обязательное поле таблицы: у реестровой строки там
            # формула, у этой пусть будет хотя бы имя метода.
            "formula": str(зап.get("formula") or "расчёт скриптом").strip(),
            # ⚠️ ДОПУЩЕНИЯ. Дисконтирование не бывает без ставки, а ставки нет в
            # отчёте по природе. Не объявил — говорим об этом в самой строке,
            # чтобы молчание не выглядело точностью.
            "assumptions": str(зап.get("assumptions") or "").strip()
                           or "ДОПУЩЕНИЯ НЕ ОБЪЯВЛЕНЫ — проверьте вводные",
            "source": "script",
        }
        готовые.append(строка)
    return готовые


def _run(metrics_path: str, cell: str, only_raw: str, out_json: str, final: bool,
         extra_calculated: str = "", script: str = "") -> int:
    from finengine.llm import station_progress as _sp
    from finengine.skills.calc import CalcError
    from finengine.skills.metric_schema import registry_for_profile
    from finengine.skills.ratios import compute_ratios, own_calc_defs

    # Чистый счёт скриптом (решение владельца 31.08.2026, «сложи 2+2»): без
    # величин из отчёта входной файл — обряд, а не вход. Файл необязателен,
    # когда есть --script; результат тогда пишется в свой файл по умолчанию.
    if not metrics_path:
        if not script:
            print("🔴 ОТКАЗ: нет входного файла (--in). Без --script считать "
                  "не из чего", file=sys.stderr)
            return EXIT_BAD_INPUT
        envelope = {"metrics": [], "run": {}}
        if not out_json:
            out_json = "./calc.json"
    else:
        src = Path(metrics_path)
        if not src.is_file():
            print(f"🔴 ОТКАЗ: нет файла таблицы прогона '{metrics_path}'", file=sys.stderr)
            return EXIT_BAD_INPUT
        try:
            envelope = _load_envelope(src)
        except ValueError as exc:
            print(f"🔴 ОТКАЗ: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT

    run = envelope.get("run") or {}
    profile_id = (cell or run.get("cell") or "").strip()
    if not profile_id and not script:
        print("🔴 ОТКАЗ: в конверте нет ячейки (run.cell) и не задан --cell",
              file=sys.stderr)
        return EXIT_BAD_INPUT

    if profile_id:
        try:
            reg = registry_for_profile(profile_id)
        except FileNotFoundError:
            print(f"🔴 ОТКАЗ: нет файла реестра ячейки '{profile_id}'", file=sys.stderr)
            return EXIT_BROKEN_REGISTRY
        except ValueError as exc:
            print(f"🔴 ОТКАЗ: реестр ячейки '{profile_id}' сломан: {exc}", file=sys.stderr)
            return EXIT_BROKEN_REGISTRY
    else:
        # Ячейки нет, но есть скрипт: реестровые формулы недоступны, считаем
        # только строки скрипта. Молчать нельзя — иначе «почему нет ROE» будет
        # выглядеть отказом движка, а не отсутствием ячейки.
        reg = {"calculated": [], "extractable": []}
        print("… ячейка не задана: реестровые формулы недоступны, считаю "
              "только скриптом", file=sys.stderr, flush=True)

    # СВОИ ФОРМУЛЫ ЧЕЛОВЕКА (решение владельца 23.08.2026). Реестр ячейки знает
    # полсотни коэффициентов, но спрашивают и те, которых в нём нет: из пяти,
    # о которых владелец спросил по Северстали, четыре отсутствовали. Раньше
    # модель считала их в уме и писала текстом — в таблицу они не попадали, то
    # есть результат нельзя было ни выгрузить, ни проверить, ни пересчитать.
    #
    # Проверка и отбор — общие с реестром (`own_calc_defs` → `CalcDef`), в файл
    # реестра ничего не пишется.
    свои: list[dict] = []
    if extra_calculated:
        сырьё = json.loads(Path(extra_calculated).read_text(encoding="utf-8"))
        сырьё = сырьё if isinstance(сырьё, list) else (сырьё.get("calculated") or [])
        известные = {str(c[0]).strip().upper() for c in reg["calculated"]}
        известные |= {str(m.get("enum", "")).strip().upper() for m in reg["extractable"]}
        свои, отказы = own_calc_defs(сырьё, известные)
        if свои:
            print(f"… свои формулы: {', '.join(str(d['enum']) for d in свои)}",
                  file=sys.stderr, flush=True)
        for причина in отказы:
            # ⚠️ ОТКАЗ НАЗЫВАЕТСЯ ВСЛУХ: молча выкинутая формула неотличима от
            # «не посчиталось из-за входов», и человек чинил бы не то.
            print(f"… своя формула не принята — {причина}", file=sys.stderr, flush=True)

    # --only: слова человека или готовые имена реестра — тот же мост, что у
    # извлечения. Своего разбора здесь нет намеренно: два разных понимания слова
    # «выручка» в одном ходе хуже, чем непонятое слово.
    only: list[str] = []
    if only_raw.strip() and not profile_id:
        # Без ячейки мосту не с чем сверять — реестровой части нет вовсе.
        print("… --only без ячейки не применим: считаю только строки скрипта",
              file=sys.stderr, flush=True)
        only = ["__НИЧЕГО_ИЗ_РЕЕСТРА__"]
    elif only_raw.strip():
        from finengine.skills.requested_metrics import (
            RequestShapeError,
            check_shape,
            resolve_requested,
        )
        asked = [w.strip() for w in only_raw.split(",") if w.strip()]
        try:
            check_shape(asked)
        except RequestShapeError as exc:
            print(f"🔴 ОТКАЗ: --only {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
        only, unknown = resolve_requested(asked, profile_id)
        # 🔴 СВОИ ИМЕНА `--only` ЗНАТЬ ОБЯЗАН (найдено прогоном обвязки до
        # выкатки, 23.08). Мост `resolve_requested` сверяется с РЕЕСТРОМ, а
        # своих формул в нём нет по устройству — и `--only ROCE_OWN` вместе со
        # своей же формулой давал «не опознано ни одного показателя» и отказ.
        # То есть новая способность выключала сама себя ровно в том случае,
        # ради которого заведена: человек просит ПЯТЬ коэффициентов, четырёх в
        # реестре нет, модель подаёт их формулами и сужает вывод до них.
        свои_имена = {str(d["enum"]) for d in свои}
        for слово in list(unknown):
            если_имя = слово.strip().upper()
            if если_имя in свои_имена:
                only.append(если_имя)
                unknown.remove(слово)
        # 🔴 СВОИ ФОРМУЛЫ ЗАКАЗАНЫ САМИМ ФАКТОМ ПОДАЧИ (прогон da0b5ad4,
        # 01.09.2026). Хвост извлечения сужает расчёт заказом (`--only $MET`),
        # а имён своих формул в $MET нет по устройству — модель их несёт
        # файлом. Без этой строки сужение выкидывало бы свои строки: заказ
        # «отношение депозитов к деньгам» исполнялся бы пустотой.
        only.extend(имя for имя in sorted(свои_имена) if имя not in only)
        if unknown:
            print(f"… ВНИМАНИЕ: не опознаны и пропущены: {', '.join(unknown)}",
                  file=sys.stderr, flush=True)
        if not only and not script:
            print("🔴 ОТКАЗ: из --only не опознано ни одного показателя",
                  file=sys.stderr)
            return EXIT_BAD_INPUT
        if not only and script:
            # СО СКРИПТОМ ЭТО НЕ ОТКАЗ. `--only` сужает РЕЕСТРОВУЮ часть, а имена
            # своих строк скрипт объявляет сам в RESULT — до его исполнения их
            # знать неоткуда. «Продисконтируй» и выглядит так: `--only EV_DCF`
            # плюс скрипт, и в реестре такого имени нет по устройству.
            print("… --only не совпал с реестром: считаю только строки скрипта",
                  file=sys.stderr, flush=True)
            only = ["__НИЧЕГО_ИЗ_РЕЕСТРА__"]

    # Замыкание состава: запрошенный коэффициент тянет за собой рассчитываемые
    # входы. Просили одно — считаем и то, без чего оно не посчитается.
    запрошено: set[str] = set()
    if only:
        only, запрошено = замкнуть_по_зависимостям(only, reg["calculated"])

    rows = envelope["metrics"]
    # Считаем по тому, что прогон ПРОЧИТАЛ (§7 «читаем шире, показываем уже»):
    # извлечённые строки конверта и есть scope.read, второй фильтр здесь завёл бы
    # второго решателя состава.
    extracted = [r for r in rows if isinstance(r, dict) and not r.get("is_calculated")]
    already = {str(r.get("enum") or "").strip().upper()
               for r in rows if isinstance(r, dict) and r.get("is_calculated")}
    # Вход считается ДОБЫТЫМ, только если в строке есть число: пустая строка
    # входом не является, и назвать её в причине отказа — соврать («формула не
    # дала значения» вместо «нет CAPEX»).
    available = available_inputs(rows)

    try:
        # Ярлыки периодов передаём ИЗ КОНВЕРТА, а не даём выбрать заново: по ним
        # решается приведение к году (#146), и второй выбор развёл бы годовую
        # величину калькулятора с полугодовой в таблице извлечения.
        # Пустой вход со скриптом — чистый счёт: гонять полсотни реестровых
        # формул по нулю величин значит выдать полсотни «не посчитано» шумом.
        computed_rows = [] if (not profile_id or (script and not extracted)) else compute_ratios(
            extracted, profile_id, only=only or None, extra=свои or None,
            period_current=str(run.get("main_period") or ""),
            period_prev=str(run.get("prev_period") or ""))
    except CalcError as exc:
        print(f"🔴 ОТКАЗ: расчёт не выполнен: {exc}", file=sys.stderr)
        return EXIT_CALC_FAILED

    # Посчитанное ПРЯМО СЕЙЧАС — тоже вход: своя формула может опираться на
    # реестровую из этого же прохода.
    available = available_inputs(rows, computed_rows)

    # #163 в мягком виде: enum, уже приехавший рассчитанным, второй раз не
    # заводим — иначе под одним именем в таблице оказались бы два числа.
    fresh = [r for r in computed_rows
             if str(r.get("enum") or "").strip().upper() not in already]

    # Скрипт идёт ПОСЛЕ формул: так он видит и извлечённое, и посчитанное, и
    # может опереться на реестровый коэффициент вместо того, чтобы вывести его
    # заново своими руками.
    if script:
        try:
            строки_скрипта = _run_script(script, extracted + computed_rows, run, profile_id)
        except CalcError as exc:
            print(f"🔴 ОТКАЗ: {exc}", file=sys.stderr)
            return EXIT_CALC_FAILED
        fresh.extend(строки_скрипта)

    show = {str(e).strip().upper() for e in ((run.get("scope") or {}).get("show") or ())}
    # Явный `--only` сам задаёт срез показа: добранные по зависимостям входы
    # посчитаны, но человек их не заказывал — на экран идёт спрошенное.
    if запрошено and not show:
        show = set(запрошено)
    # 🔴 СВОЯ ФОРМУЛА — ЭТО ТО, ЧТО ЧЕЛОВЕК ПОПРОСИЛ ВСЛУХ, И ОНА ОБЯЗАНА БЫТЬ
    # НА ЭКРАНЕ (найдено прогоном обвязки до выкатки, 23.08).
    #
    # Срез показа (`scope.show`) считает ИЗВЛЕЧЕНИЕ — до того, как свои формулы
    # вообще появились. Значит своя строка выходила `in_role: False`, и её
    # отфильтровывал `table_output_enums` уже на фронте: посчитано верно
    # (ROCE 4,528 — сошлось с разметкой), а в рабочей области пусто.
    #
    # Ровно тот же класс, что мы чинили сегодня в `station_turn`: срез считает
    # один, а дополняет состав другой. Дополняем ЗДЕСЬ — здесь и только здесь
    # известны оба: и срез из конверта, и принятые свои имена.
    if свои and show:
        свои_имена = {str(d["enum"]) for d in свои}
        show |= свои_имена
        область = run.setdefault("scope", {})
        область["show"] = sorted({*(область.get("show") or ()), *свои_имена})
        область["show_total"] = len(область["show"])
    deps_by_enum = deps_map(reg["calculated"], свои)

    # ОДНО ИМЯ — ОДНА СТРОКА, с какого бы конца значение ни пришло (#163).
    # Пустую строку показателя ставит извлечение («искали и не нашли»);
    # посчитанное её ЗАМЕЩАЕТ, а не ложится рядом. Если и формула промолчала,
    # второй пустой строки не заводим — переносим в первую причину отказа.
    empty_at = {str(r.get("enum") or "").strip().upper(): i
                for i, r in enumerate(rows)
                if isinstance(r, dict) and not r.get("found") and not r.get("dimension")}

    ok, skipped = [], []
    for row in fresh:
        enum = str(row.get("enum") or "").strip().upper()
        # in_role — метка ПОКАЗА, не расчёта: срез роли решает, что увидит
        # пользователь, а в файл едет всё (анализу нужен полный базис).
        row["in_role"] = (enum in show) if show else True
        row["type"] = "metric"
        at = empty_at.get(enum)
        if row.get("found"):
            ok.append(row)
        else:
            # Причина отказа хранится В СТРОКЕ, а не только печатается: в stdout
            # помещается дюжина строк, а разбирать «почему пусто» будут по файлу.
            row["calc_missing"] = _missing_inputs(deps_by_enum.get(enum, []), available)
            skipped.append(row)
            if at is not None:
                rows[at]["calc_missing"] = row["calc_missing"]
                rows[at]["formula"] = rows[at].get("formula") or row.get("formula") or ""
                continue
        if at is not None:
            rows[at] = row
        else:
            rows.append(row)
        _sp.emit(row)

    # EvidenceBundleV2 (Срез 2, B2): пересобираем поверх ОБНОВЛЁННОГО конверта
    # (`rows` — тот же список, что `envelope["metrics"]`, домешаны свежие
    # расчётные строки) — так `formula_engine` кладёт свои `DerivedFact` в ТУ
    # ЖЕ доказательную базу, что и извлечение, без второго файла и без
    # пересчёта. Без `run_passport.run_id` (чистый счёт скриптом — `--script`
    # без входного файла) собирать бандл не с чем, и это не отказ, а честное
    # «для этого прогона паспорта нет». Best-effort по тому же принципу, что
    # и в `extract.py`: вторичный слой не имеет права уронить расчёт.
    run_id = (run.get("run_passport") or {}).get("run_id")
    if run_id:
        try:
            from finengine.skills.evidence_bundle import evidence_bundle_from_envelope
            bundle = evidence_bundle_from_envelope(run_id, envelope)
            envelope["evidence_bundle"] = json.loads(bundle.model_dump_json())
        except Exception as exc:  # noqa: BLE001 — вторичный слой, см. комментарий выше
            print(f"… EvidenceBundleV2 не собран (не блокирует расчёт): {exc}",
                  file=sys.stderr, flush=True)

    dst = Path(out_json or metrics_path)
    _write_atomically(dst, envelope)

    # В stdout — ТОЛЬКО срез роли: он и есть то, что увидит пользователь, а
    # полсотни строк про формулы вне его раскладки — простыня в контекст модели
    # (#207). В файл при этом уехало всё: анализу нужен полный базис.
    shown_ok = [r for r in ok if r.get("in_role")]
    shown_skipped = [r for r in skipped if r.get("in_role")]
    print(f"Рассчитано кодом: {len(ok)} из {len(fresh)} формул "
          f"(ячейка {profile_id}, таблица {dst})")
    for row in shown_ok:
        unit = f" {row['unit']}" if row.get("unit") else ""
        print(f"  {row.get('enum')} = {row.get('value_current')}{unit}")
    заказано = {str(e).strip().upper() for e in ((run.get("scope") or {}).get("read") or ())}
    рыночные = {str(d.get("enum", "")).strip().upper() for d in свои
                if str(d.get("source", "")).strip().lower() == "market"}
    for row in shown_skipped[:_SKIPPED_LINES]:
        missing = row.get("calc_missing") or []
        if not missing:
            print(f"  не посчитано: {row.get('enum')} — формула не дала значения")
            continue
        print(f"  не посчитано: {row.get('enum')}")
        for строка in _diagnose(missing, заказано, рыночные):
            print(f"      {строка}")
    hidden = len(skipped) - min(len(shown_skipped), _SKIPPED_LINES)
    if hidden:
        print(f"  ещё {hidden} формул не посчиталось — причины по каждой в файле {dst}")

    if final:
        # Терминал ставит ПОСЛЕДНИЙ скрипт цепочки и ставит всегда: сводку хода
        # собирает адресат из `done.result`, а строки извлечения уехали событиями
        # раньше. Несём вперёд конверт извлечения — иначе сводка обеднела бы до
        # двух счётчиков расчёта.
        _sp.emit({"type": "done", "result": {
            **run,
            "calculated_computed": len(ok),
            "calculated_skipped": len(skipped),
            # 🔴 СВОИ ФОРМУЛЫ — В ТЕРМИНАЛ, ЧТОБЫ ПЕРЕЖИТЬ КОМНАТУ (Р1, 24.08).
            # Файл `--extra-calculated` умирает вместе с комнатой через двадцать
            # минут. Следующим ходом «на первом шаге я написала A/B, а нужно
            # A/(B+C)» править нечего — первой формулы уже нет. Конверт прогона
            # хранилище сохраняет, и оттуда её видно.
            **({"own_calculated": свои} if свои else {}),
        }})
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="AIFA CLAW ratios (station): коэффициенты по таблице прогона")
    # `--in` — основное имя (решение владельца 31.08.2026: `--metrics` здесь
    # означал ПУТЬ К ФАЙЛУ, а в извлечении тот же флаг — список имён; одно
    # слово с двумя смыслами путало и человека, и модель). Старое имя живо
    # алиасом: его зовёт хвост extract_run.sh и старые сессии.
    ap.add_argument("--in", "--metrics", dest="metrics", default="",
                    help="входной файл набора величин {run, metrics, …} — его "
                         "пишет извлечение (--out-json) или составь сам. "
                         "Обязателен всегда, кроме чистого счёта скриптом")
    ap.add_argument("--extra-calculated", default="",
                    help="файл со СВОИМИ формулами (JSON-список записей реестра: "
                         "enum, name_ru, formula, deps, unit_kind, annualize)")
    ap.add_argument("--cell", default="",
                    help="ячейка реестра; нужна только для ЧУЖОЙ таблицы, "
                         "в своей она уже записана в run.cell")
    ap.add_argument("--only", default="",
                    help="считать только эти показатели (имена реестра или слова "
                         "человека, через запятую)")
    ap.add_argument("--out-json", default="",
                    help="куда писать конверт; по умолчанию — тот же файл")
    ap.add_argument("--script", default="",
                    help="файл с питоном для расчётов, которые формулой не "
                         "записать: дисконтирование, IRR, CAGR, сценарии. "
                         "Строки для таблицы скрипт объявляет в RESULT "
                         "(ОТНОСИТЕЛЬНЫЙ путь)")
    ap.add_argument("--final", action="store_true",
                    help="поставить терминальное событие хода: этот скрипт "
                         "последний в цепочке extract ; calc")
    a = ap.parse_args()
    sys.exit(_run(a.metrics, a.cell, a.only, a.out_json, a.final,
                  a.extra_calculated, a.script))


if __name__ == "__main__":
    main()
