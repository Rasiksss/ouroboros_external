"""Коэффициенты реестра по готовой таблице показателей — публичная дверь.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Канон (docs/SKILLS_ARCHITECTURE.md §4, шаг 4) делает
расчёт коэффициентов ОТДЕЛЬНЫМ шагом маршрута: скрипт навыка `aifa-calc`
берёт таблицу прогона файлом и дописывает в неё расчётные строки. Сегодня та же
работа делается ВНУТРИ движка (`bulk_extractor`), приватными функциями, до
которых снаружи не дотянуться иначе как импортом подчёркнутого имени.

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО ЗДЕСЬ НЕТ. Здесь НЕТ ни одной формулы и ни одного нового
правила: считает тот же `_calculate_metrics_*`, единицу подписывает тот же
`unit_for_calculated`, к году приводит тот же `annualized`. Вторая реализация
арифметики — это два числа под одним именем, и мы уже знаем, чем это кончается
(#163). Здесь есть ровно перекладка: строки таблицы → входы вычислителя →
строки таблицы обратно, в той же движковой форме.

ЧТО ЗНАЧИТ «ТОЙ ЖЕ ФОРМЫ». Строка расчёта неотличима от строки движка: те же
ключи, `is_calculated: true`, заполненный `formula`. По этому признаку её
исключает из счётчика прогресса `analyze.py::_is_extractable_row`, по нему же
её узнаёт раскладка роли. Придумать здесь свою форму значило бы переписать
всех потребителей таблицы.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _refuse_unknown_calls(formula: str) -> None:
    """Формула зовёт функцию, которой у движка нет, — отказ СРАЗУ и вслух.

    🔴 ЖИВОЙ ПРОГОН ВЛАДЕЛЬЦА 24.08.2026. Модель написала свою формулу чистого
    долга через `COALESCE(SHORT_TERM_LEASE_LIABILITIES, 0)` — синтаксис из SQL,
    которого у нас нет. Движок такой вызов отвергает
    (`formula_engine._eval_formula_node`), но глубоко: наружу это выходит
    строкой БЕЗ ЗНАЧЕНИЯ, неотличимой от честного «входов не нашлось». Человек
    видит пустую клетку и думает, что аренды нет в отчёте, — а на самом деле
    формула нерабочая.

    Проверять на приёме, а не при счёте, — разница в пять минут прогона и в
    понятности отказа: здесь мы ещё можем НАЗВАТЬ причину и перечислить, что
    разрешено, и модель поправит формулу до запуска.

    ⚠️ СПИСОК РАЗРЕШЁННОГО НЕ ДУБЛИРУЕМ — берём у самого движка. Своя копия
    здесь разошлась бы с ним при первой правке, и отказ начал бы врать.

    🔴 БРАЛИ НЕ ТОТ СПИСОК (до 29.08.2026). Стоял `_FORMULA_FUNCS` — вызовы,
    которые исполняет обходчик AST. Но проверка идёт ДО раскрытия макросов, а
    `AVG` и `ANNUAL` раскрываются раньше разбора и в тот список не входят по
    построению. Итог: реестровые формулы с `AVG(...)`/`ANNUAL(EBITDA)` считались,
    а такая же формула МОДЕЛИ отвергалась — при том что тело навыка `aifa-extract`
    само разрешало ей `ANNUAL` и давало пример с `AVG`. Теперь сверяемся с
    `FORMULA_CALLABLES` — «что можно звать», а не «что считает обходчик».
    """
    import ast

    from finengine.skills.bulk_extractor.formula_engine import (
        FORMULA_CALLABLES,
        _normalise_formula_expr,
    )

    дерево = ast.parse(_normalise_formula_expr(formula), mode="eval")
    for узел in ast.walk(дерево):
        if not isinstance(узел, ast.Call):
            continue
        имя = getattr(узел.func, "id", "?")
        if имя not in FORMULA_CALLABLES:
            raise ValueError(
                f"формула зовёт «{имя}(…)», а движок такого не знает. Разрешены "
                f"только {'/'.join(sorted(FORMULA_CALLABLES))}. Пропущенное значение "
                "подставлять нулём не нужно: показатель без входа просто не "
                "посчитается, и это будет видно")


def own_calc_defs(rows: list[dict], known: set[str]) -> tuple[list[dict], list[str]]:
    """Свои формулы человека → записи реестра. Возвращает (принятые, отказы).

    🔴 ЗАЧЕМ ЭТО ЕСТЬ (решение владельца 23.08.2026). `--extra-metrics` умеет
    только ИЗВЛЕКАЕМЫЕ показатели: описал, где искать в отчёте, — движок нашёл.
    Коэффициента так не завести, а спрашивают именно их: из пяти, о которых
    владелец спросил по Северстали, четыре (ROCE, FCF на акцию, чистый долг с
    арендой, запас по ковенанту) в реестре ячейки отсутствуют. Модель считала
    их в уме и писала текстом, в таблицу они не попадали вовсе — то есть
    результат работы нельзя было ни выгрузить, ни проверить, ни пересчитать.

    ⚠️ РЕЕСТР ВЫИГРЫВАЕТ СТОЛКНОВЕНИЕ ИМЁН — как и у `--extra-metrics`, и по той
    же причине: там определение выверено, а модель могла придумать своё под тем
    же именем, и подмена была бы невидимой.

    ⚠️ ПРОВЕРЯЕМ ТЕМ ЖЕ ВАЛИДАТОРОМ, ЧТО И ПОСТАВЛЯЕМЫЙ РЕЕСТР (`CalcDef`).
    Своя проверка здесь была бы вторым набором правил: запрет двойного
    приведения к году (`annualize` вместе с `ANNUAL(...)`, ×16 на квартале)
    живёт в нём, и обойти его через свою формулу нельзя.

    В реестр ничего не пишется: записи живут в памяти прогона, как и свои
    извлекаемые показатели.
    """
    from finengine.skills.metric_schema import CalcDef

    принятые: list[dict] = []
    отказы: list[str] = []
    for row in rows or []:
        имя = str((row or {}).get("enum") or "").strip().upper()
        if not имя:
            отказы.append(f"{str(row)[:40]}: без имени")
            continue
        if имя in known:
            отказы.append(f"{имя}: уже есть в реестре ячейки")
            continue
        if not str((row or {}).get("formula") or "").strip():
            отказы.append(f"{имя}: без формулы")
            continue
        try:
            CalcDef(**{**row, "enum": имя})
            _refuse_unknown_calls(str(row.get("formula") or ""))
        except Exception as exc:  # noqa: BLE001 — текст отказа уходит человеку
            # ⚠️ БЕРЁМ СТРОКУ С ПРИЧИНОЙ, А НЕ ПОСЛЕДНЮЮ. У pydantic последняя —
            # это ссылка на документацию (`errors.pydantic.dev/…`), и отказ
            # выглядел бы как «формула не принята: посетите сайт».
            строки = [s.strip() for s in str(exc).splitlines() if s.strip()]
            полезные = [s for s in строки
                        if "pydantic.dev" not in s and "validation error" not in s
                        and not s.startswith("[type=")]
            отказы.append(f"{имя}: {' '.join(полезные)[:200] or str(exc)[:200]}")
            continue
        принятые.append({**row, "enum": имя})
    return принятые, отказы


def _with_calculated_deps(wanted: set[str], calculated: list) -> set[str]:
    """Расширить `only` рассчитываемыми входами запрошенного.

    🔴 НАЙДЕНО ПРОГОНОМ ПО НОВАТЭКУ 29.08.2026. Коэффициент реестра может стоять
    на ДРУГОМ рассчитываемом:

        NET_DEBT       = TOTAL_DEBT − CASH_EQUIVALENTS − BANK_DEPOSITS
        FREE_CASH_FLOW = NET_OPERATING_CASH_FLOW − CAPEX

    `only` сужал состав буквально, поэтому `only=["NET_DEBT"]` не считал
    `TOTAL_DEBT` и возвращал ПУСТО. Тот же расчёт без сужения давал 80 961 —
    то есть «посчитай чистый долг» отдельной просьбой не работало, а хвостом
    прогона работало.

    Диагноз при этом уводил в сторону: «TOTAL_DEBT — не заказывали (нужно
    извлечение)». Извлечение рассчитываемый показатель не добудет никогда, и
    модель по такому совету уходила переизвлекать документ.

    Живёт ЗДЕСЬ, а не в скрипте навыка, потому что `compute_ratios` зовут трое:
    станционный расчёт, извлечение и скрипт, который пишет модель. У последнего
    ловушка была бы та же, а подсказать ему некому.
    """
    имена, deps = set(), {}
    for t in calculated:
        имя = (t[0] if isinstance(t, (list, tuple)) else str(t.get("enum") or "")).strip().upper()
        if not имя:
            continue
        имена.add(имя)
        сырьё = (t[3] if isinstance(t, (list, tuple)) and len(t) >= 4
                 else (t.get("deps") if isinstance(t, dict) else None)) or ()
        deps[имя] = [str(d).strip().upper() for d in сырьё]
    состав, очередь = set(wanted), list(wanted)
    while очередь:
        for d in deps.get(очередь.pop(), ()):
            # Добираем только рассчитываемые: извлекаемый вход взять неоткуда.
            if d in имена and d not in состав:
                состав.add(d)
                очередь.append(d)
    return состав


def compute_ratios(
    metrics: list[dict],
    profile_id: str,
    *,
    only: list[str] | None = None,
    period_current: str = "",
    period_prev: str = "",
    extra: list[dict] | None = None,
    registry_override: dict | None = None,
) -> list[dict]:
    """Расчётные строки по реестру ячейки поверх уже извлечённой таблицы.

    `metrics` — строки движка (`enum`, `period_values`, `value_current`,
    `value_prev`, `is_calculated`). Сегментные и групповые строки (`dimension`)
    во входы расчёта НЕ идут: у них своя размерность и свой разрез, и подмешать
    их значило бы посчитать рентабельность компании по одному сегменту.

    `only` — сузить состав до перечисленных enum'ов. Пусто — весь реестр.

    `period_current` / `period_prev` — ярлыки основного и сравнительного
    периодов. ⚠️ ЭТИХ ДВУХ ПАРАМЕТРОВ НЕТ В ДВИЖКЕ: там их выбирает
    `_select_run_periods` по тексту отчёта, а сюда текста не приходит. Не
    передали — берём два свежайших периода таблицы. Ярлык нужен не для
    красоты: по нему `annualized` решает, умножать ли полугодовую величину на
    два, и ошибка в нём — ровно тот вдвое худший банк из #146.
    """
    from finengine.skills.bulk_extractor import (
        _calculate_metrics_for_periods,
        _calculate_metrics_pair,
        _calculated_period_kind_map,
        _value_for_period,
        _with_point_values_for_duration_calculations,
        annualization_note,
        annualized,
        sort_periods,
        unit_for_calculated,
    )
    from finengine.skills.bulk_extractor import POINT_KIND as _POINT_KIND
    from finengine.skills.extract_result import dominant_unit
    from finengine.skills.metric_schema import registry_for_profile

    # Passport-aware callers pass the already-frozen effective registry. The
    # default preserves every legacy caller, but a running graph must never
    # re-read a newer formula after its RunPassport was created.
    registry = registry_override if registry_override is not None else registry_for_profile(profile_id)
    if extra:
        # Свои формулы идут ПОСЛЕ реестровых и не перебивают их: отбор по имени
        # уже сделан в `own_calc_defs`, здесь только дописываем.
        registry = {
            **registry,
            "calculated": [*registry["calculated"],
                           *[(d["enum"], d.get("name_ru") or d["enum"],
                              d.get("formula") or "", list(d.get("deps") or []))
                             for d in extra]],
            "calculated_meta": {
                **(registry.get("calculated_meta") or {}),
                **{d["enum"]: {"unit_kind": d.get("unit_kind", ""),
                               "annualize": bool(d.get("annualize")),
                               "min_value": d.get("min_value"),
                               "max_value": d.get("max_value")} for d in extra},
            },
        }
    flat = [m for m in (metrics or [])
            if str(m.get("enum") or "").strip() and not m.get("dimension")]

    # Показатель, уже стоящий в таблице, второй раз не заводим. Это то самое
    # смягчение #163: шесть enum'ов реестра заведены И извлекаемыми, И
    # расчётными, и посчитать их поверх извлечённых значило бы положить в одну
    # таблицу две строки с одним именем и разными числами.
    # 🔴 «УЖЕ СТОИТ В ТАБЛИЦЕ» — ЭТО СТРОКА СО ЗНАЧЕНИЕМ (19.08, Б0). Пустая
    # строка (искали и не нашли) формулу не отменяет: ровно так пропала EBITDA
    # Северстали при извлечённых операционной прибыли 28 355 и амортизации
    # 9 495. Смягчение #163 при этом остаётся в силе — двух чисел под одним
    # именем не будет: посчитанное ЗАМЕЩАЕТ пустую строку, а не ложится рядом
    # (`aifa-calc/scripts/calc_run.py`).
    present = {str(m.get("enum")).strip().upper() for m in flat
               if m.get("found") or m.get("value_current") is not None}
    wanted = {str(e).strip().upper() for e in (only or []) if str(e).strip()}
    if wanted:
        wanted = _with_calculated_deps(wanted, registry["calculated"])
    calc_defs = [
        d for d in registry["calculated"]
        if str(d[0]).strip().upper() not in present
        and (not wanted or str(d[0]).strip().upper() in wanted)
    ]
    if not calc_defs:
        return []

    calc_meta = registry.get("calculated_meta") or {}
    default_unit = dominant_unit([m for m in flat if not m.get("is_calculated")])
    formula_default_abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in registry["extractable"]
        if m.get("formula_default_abs_behavior")
    }

    extracted_by_period: dict[str, dict] = {}
    extracted_current: dict[str, float | None] = {}
    extracted_prev: dict[str, float | None] = {}
    for row in flat:
        enum = str(row.get("enum")).strip().upper()
        for pv in (row.get("period_values") or []):
            if pv.get("value") is not None:
                extracted_by_period.setdefault(str(pv.get("period") or ""), {})[enum] = pv["value"]
        extracted_current[enum] = row.get("value_current")
        extracted_prev[enum] = row.get("value_prev")

    all_periods = sort_periods([p for p in extracted_by_period if p])
    if not period_current:
        period_current = all_periods[0] if all_periods else ""
    if not period_prev:
        period_prev = all_periods[1] if len(all_periods) > 1 else ""

    # ПРОИСХОЖДЕНИЕ ЕДЕТ ВТОРЫМ КАНАЛОМ (Б5, 19.08.2026): числа возвращаются
    # прежним словарём, а «по какой формуле и по каким входам» — этими тремя.
    # Каналов три, потому что значение строки берётся из трёх мест, и подписать
    # строку происхождением ДРУГОГО прохода значило бы соврать.
    trace_current: dict[str, dict] = {}
    trace_calc_by_period: dict[str, dict] = {}
    trace_display_by_period: dict[str, dict] = {}
    calc_current, calc_prev = _calculate_metrics_pair(
        calc_defs, extracted_current, extracted_prev,
        formula_default_abs_enums=formula_default_abs_enums,
        trace_current=trace_current, period_current=period_current,
        period_prev=period_prev)
    # Два прохода по периодам, как в движке: точечные (балансовые) показатели
    # считаются по НЕдополненной таблице, длительностные — по дополненной
    # балансовыми значениями. Один проход на всех дал бы среднее там, где нужен
    # остаток на дату.
    calc_by_period = _calculate_metrics_for_periods(
        calc_defs,
        _with_point_values_for_duration_calculations(extracted_by_period, all_periods),
        all_periods, formula_default_abs_enums=formula_default_abs_enums,
        trace_by_period=trace_calc_by_period)
    display_calc_by_period = _calculate_metrics_for_periods(
        calc_defs, extracted_by_period, all_periods,
        formula_default_abs_enums=formula_default_abs_enums,
        trace_by_period=trace_display_by_period)
    period_kinds = _calculated_period_kind_map(calc_defs)

    rows: list[dict] = []
    for enum, name_ru, formula, _deps in calc_defs:
        key = str(enum).strip().upper()
        meta = calc_meta.get(key, {})
        unit_calc = unit_for_calculated(formula, default_unit, meta.get("unit_kind", ""))
        do_annualize = bool(meta.get("annualize"))
        source = (display_calc_by_period if period_kinds.get(key) == _POINT_KIND
                  else calc_by_period)
        source_trace = (trace_display_by_period if period_kinds.get(key) == _POINT_KIND
                        else trace_calc_by_period)
        period_values = [
            {"period": p,
             "value": annualized(source.get(p, {}).get(enum), p, do_annualize),
             "unit": unit_calc, "page": None, "source_text": ""}
            for p in all_periods
            if source.get(p, {}).get(enum) is not None
        ]
        v_cur = calc_current.get(enum)
        v_cur = (_value_for_period(period_values, period_current) if v_cur is None
                 else annualized(v_cur, period_current, do_annualize))
        v_prv = calc_prev.get(enum)
        v_prv = (_value_for_period(period_values, period_prev) if v_prv is None
                 else annualized(v_prv, period_prev, do_annualize))
        # Происхождение берём из ТОГО ЖЕ прохода, что дал `v_cur`: парный
        # проход отметился в `trace_current` ровно тогда, когда вернул число,
        # а иначе значение пришло колонкой `period_current` — по совпадению
        # подписи она же в `period_values`, и её проход знает свои входы.
        prov = trace_current.get(enum) or source_trace.get(period_current, {}).get(enum) or {}
        # Приведение к году делается ПРИ ВЫДАЧЕ, а входы сняты за период: без
        # множителя в подписи арифметика «входы → напечатанное» не сходится, и
        # у 40 показателей реестра расхождение выглядит ошибкой расчёта.
        note = annualization_note(period_current) if do_annualize else ""
        formula_shown = prov.get("formula") or formula
        rows.append({
            "type": "metric",
            "enum": enum,
            "name_ru": name_ru,
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": unit_calc,
            "found": v_cur is not None,
            "is_calculated": True,
            "formula": f"({formula_shown}) * {note}" if note else formula_shown,
            "inputs": prov.get("inputs") or [],
            # 🔴 ПРОВЕРКА ПРАВДОПОДОБИЯ СВОЕЙ ФОРМУЛЫ (Р7, 24.08.2026).
            #
            # Вопрос владельца: «написала формулу, значение получилось 500
            # миллиардов, выглядит подозрительно — чтобы она могла себя
            # перепроверить». У реестровых показателей диапазоны есть
            # (effective registry), у своей формулы взяться им неоткуда:
            # это НАШ список, и чужого показателя в нём не бывает.
            #
            # Границы объявляет автор формулы — модель, когда её сочиняет. Поле
            # то же, что у реестровых (`sanity`), поэтому дальше по цепочке
            # (карточка, разбор, журнал) ничего менять не пришлось.
        })
        # ⚠️ ПОЛЕ СТАВИМ, ТОЛЬКО ЕСЛИ ЕСТЬ ЧТО СКАЗАТЬ. Пустое `sanity` в каждой
        # строке ломает паритет с боевым путём движка
        # (`test_the_live_engine_agrees_with_the_calc_skill`), а состав полей у
        # двух путей обязан совпадать: расхождение однажды разведёт форматы
        # молча. Границы бывают только у СВОИХ формул, которых на боевом пути не
        # бывает вовсе, — значит и лишнего поля там не появится.
        note_sanity = _own_sanity(meta, v_cur, unit_calc)
        if note_sanity:
            rows[-1]["sanity"] = note_sanity
    return rows


def _own_sanity(meta: dict, value: object, unit: str) -> str:
    """Значение своей формулы вне объявленного автором диапазона.

    Пусто — либо границ не объявляли (законно: не у всякой величины есть
    осмысленный размах), либо значение в них укладывается.
    """
    низ, верх = meta.get("min_value"), meta.get("max_value")
    if value is None or (низ is None and верх is None):
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if низ is not None and v < float(низ):
        return (f"ниже объявленной границы {низ}{unit and ' ' + unit}: "
                f"получено {v:.4g} — проверь формулу и входы")
    if верх is not None and v > float(верх):
        return (f"выше объявленной границы {верх}{unit and ' ' + unit}: "
                f"получено {v:.4g} — проверь формулу и входы")
    return ""
