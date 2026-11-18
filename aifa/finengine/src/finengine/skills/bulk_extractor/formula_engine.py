"""Вычисление расчётных показателей по формулам реестра.

Считает КОД, а не модель — решение владельца 09.08: код возит и считает числа,
модель решает смысл. Разбор формулы, размерность результата, единица,
аннуализация, усреднение остатков по периодам.
"""
from __future__ import annotations

import ast
import re
from collections import deque
from typing import Callable, Optional
from ._log import _LOG
from finengine.skills.periods import DURATION_KIND, POINT_KIND, normalize_period_label, period_kind_for_enum, period_sort_key, sort_periods
from .period_selection import _period_boundary_keys, _period_end_key, _point_period_key


_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]*\b")

# `ANNUAL` и `DAYS_IN_PERIOD` — имена ВРЕМЕНИ РАСЧЁТА: длину периода даёт метка
# колонки, а не отчёт, поэтому входами показателя они не числятся и в `deps`
# реестра не попадают.
_NON_METRIC_DEPS = {"AVG", "ABS", "MIN", "MAX", "ANNUAL", "DAYS_IN_PERIOD"}

_OPTIONAL_ZERO_DEPS_BY_ENUM = {
    "TOTAL_CASH_EQUIVALENTS": {"BANK_DEPOSITS"},
    "NET_DEBT": {"BANK_DEPOSITS"},
}

_PARTIAL_ZERO_FORMULA_ENUMS = {"CAPEX", "DIVIDENDS", "TOTAL_DEBT"}

class _MissingFormulaInput(Exception):
    """Raised when a calculated formula cannot be evaluated for this period."""

def _formula_metric_tokens(formula: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalise_formula_expr(formula))) - _NON_METRIC_DEPS

def _numeric_env_keys(
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]] | None = None,
) -> set[str]:
    keys: set[str] = set()
    for source in (calculated or {}, extracted):
        for key, value in source.items():
            if value is None:
                continue
            try:
                float(value)
            except Exception:
                continue
            keys.add(str(key))
    return keys

def _formula_allows_partial_zero(formula: str) -> bool:
    expr = _normalise_formula_expr(formula)
    if "/" in expr or "*" in expr:
        return False
    return not re.search(r"\b(?:AVG|MIN|MAX)\s*\(", expr, re.I)

def _collect_added_tokens(node: ast.AST, sign: int, out: set[str]) -> None:
    """Обойти дерево формулы, складывая имена, стоящие под знаком «плюс».

    Разбор дерева, а не регуляркой по строке: в «A - (B + C)» скобка меняет
    знак сразу двум слагаемым, и посимвольный счёт минусов это пропускает.
    """
    if isinstance(node, ast.Expression):
        _collect_added_tokens(node.body, sign, out)
    elif isinstance(node, ast.BinOp):
        _collect_added_tokens(node.left, sign, out)
        _collect_added_tokens(node.right, -sign if isinstance(node.op, ast.Sub) else sign, out)
    elif isinstance(node, ast.UnaryOp):
        _collect_added_tokens(node.operand, -sign if isinstance(node.op, ast.USub) else sign, out)
    elif isinstance(node, ast.Name) and sign > 0:
        out.add(node.id)

def _added_formula_tokens(formula: str) -> set[str]:
    """Слагаемые формулы (то, что прибавляется), без вычитаемых."""
    try:
        tree = _parse_formula_expr(formula)
    except _FormulaError:
        # Реестр правится руками, и битую формулу сюда приносит редактор, а не
        # прогон. Молча вернуть пусто = «плюс-слагаемых нет» = подстановка нуля
        # выключена; сама формула всё равно упадёт на разборе ниже и даст None.
        return set()
    added: set[str] = set()
    _collect_added_tokens(tree, 1, added)
    return added - _NON_METRIC_DEPS

def _is_plain_sum_of_metrics(node: ast.AST) -> bool:
    """Дерево формулы — сложение и вычитание ИМЁН, и ничего больше.

    Проверяем деревом, а не поиском знаков в строке, по двум причинам сразу.
    У произведения и частного слагаемых нет вовсе, а `_collect_added_tokens`
    ходит по любому `BinOp` одинаково и вернул бы для `A / B` оба имени как
    «слагаемые». А число в формуле («A + 1») означает, что автор считает не
    сумму строк отчёта, и переносить на результат конвенцию строк отчёта не о
    чем — поймано сторожем `test_formula_default_abs_applies_to_inputs`.
    """
    if isinstance(node, ast.Expression):
        return _is_plain_sum_of_metrics(node.body)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        return _is_plain_sum_of_metrics(node.left) and _is_plain_sum_of_metrics(node.right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _is_plain_sum_of_metrics(node.operand)
    return isinstance(node, ast.Name) and node.id not in _NON_METRIC_DEPS

def _is_plain_sum_formula(formula: str) -> bool:
    try:
        tree = _parse_formula_expr(formula)
    except _FormulaError:
        # Битую формулу приносит редактор реестра, а не прогон: тем же способом,
        # что и `_added_formula_tokens`, отвечаем «не сумма» и не трогаем знак.
        return False
    return _is_plain_sum_of_metrics(tree)

def _result_is_outflow(formula: str, env: dict[str, float], abs_applied: set[str]) -> bool:
    """Сумма модулей оттоков — тоже отток, и печатать её плюсом нельзя.

    ЦЕНА (прогон владельца по Северстали Q1-2025, 20.08.2026). CAPEX приехал
    +43 591 при размеченных −43 591, и то же за Q1 2024. Показатель заведён И
    извлекаемым, И расчётным: найдена печатная строка — в таблицу едет число со
    знаком отчёта, `(43 591)` → −43 591; не найдена — считает формула
    `ASSET_PURCHASES + INTANGIBLE_ASSET_PURCHASES - ASSET_SALES_PROCEEDS`, и
    признак `formula_default_abs_behavior` снимает знак с обеих покупок. Сумма
    модулей выезжает плюсом. Одна строка таблицы меняет знак в зависимости от
    того, какая её половина сработала, — и читателю это ничем не объяснено.

    ПРАВИЛО. Признак `formula_default_abs_behavior` — про ПОДСТАНОВКУ: формулы
    реестра написаны арифметикой модулей (`FEE_INCOME - FEE_EXPENSE`), и знак у
    входа снимается, чтобы вычитаемое вычиталось. Про РЕЗУЛЬТАТ он не говорит
    ничего. Когда каждое слагаемое формулы — показатель, у которого знак сняли
    и который в отчёте стоял отрицательным, посчитан модуль оттока, и строка
    обязана уйти в ту же конвенцию, что и её извлечённая половина.

    ПОЧЕМУ «ВСЕ СЛАГАЕМЫЕ», А НЕ «ХОТЬ ОДНО». `NET_COMMISSION_INCOME =
    FEE_AND_COMMISSION_INCOME - FEE_AND_COMMISSION_EXPENSE`: слагаемое здесь
    доход, он положителен, знак результата не трогаем. `EBITDA =
    OPERATING_PROFIT + DEPRECIATION_AMORTIZATION`: у операционной прибыли
    признак снят (`false` — двунаправленная), в `abs_applied` она не попадёт
    никогда, и убыточный год не уведёт EBITDA в минус.

    ЧТО ИМЕННО ДЕЛАЕТСЯ СО ЗНАКОМ — ОТРИЦАНИЕ, А НЕ `-abs` (21.08.2026).
    Формула считает МОДУЛЬ нетто-потока в арифметике модулей; конвенция отчёта
    печатает отток минусом, то есть на экран едет `−(модуль)`. Когда модуль сам
    отрицателен, поток на самом деле ВХОДЯЩИЙ, и печатать его минусом нельзя:
    `NET_INTEREST_PAID = INTEREST_PAID − INTEREST_RECEIVED` у банка даёт −46 491
    (DNB 6M 2025) — процентов получено больше, чем уплачено. `-abs` напечатал бы
    «уплачено 46 491». На разметке таких срабатываний 23 из 261: NET_INTEREST_PAID
    21 из 50, CAPEX 2 из 81 (продажи имущества перекрыли покупки).

    Отрицание вдобавок ОБРАТИМО САМО СОБОЙ, и это несущее свойство: тот же знак
    снимается при обратной подстановке значения в чужую формулу
    (`_formula_env`), и снимается ТОЧНО, без второго правила.

    ЗАМЕР ПО ВСЕЙ РАЗМЕТКЕ (112 документов, 8 непустых корзин из десяти; мера —
    расчётная строка с эталоном сошлась с точностью 5%): 204 сошедшихся из 394
    без правила, 331 с ним. Чинит 130 — DIVIDENDS 64, CAPEX 56,
    COMBINED_ASSET_PURCHASES 10; ломает 3, все три амазоновские, где разметка
    пишет CAPEX величиной без знака (`test_..._reaches_consumers_as_a_module`).
    """
    if not _is_plain_sum_formula(formula):
        return False
    # Достроенный нулём вход в сумму ничего не вносит и права вето на знак не
    # имеет: у CAPEX без раскрытых НМА слагаемых осталось одно, и оно — отток.
    added = {token for token in _added_formula_tokens(formula) if env.get(token)}
    return bool(added) and added <= abs_applied

def _optional_zero_deps_for_formula(
    enum: str,
    formula: str,
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]] | None = None,
) -> set[str]:
    optional_zero_deps = set(_OPTIONAL_ZERO_DEPS_BY_ENUM.get(enum, set()))
    if enum not in _PARTIAL_ZERO_FORMULA_ENUMS:
        return optional_zero_deps
    tokens = _formula_metric_tokens(formula)
    if not tokens or not _formula_allows_partial_zero(formula):
        return optional_zero_deps
    present = _numeric_env_keys(extracted, calculated) & tokens
    # 🔴 ЗАЩИТА ОТ ВЕЛИЧИНЫ, СОБРАННОЙ ИЗ ОДНОГО ВЫЧИТАЕМОГО (20.08.2026).
    #
    # Подстановка нуля задумана для НЕДОСТАЮЩЕГО СЛАГАЕМОГО: компания не
    # раскрыла покупку НМА — считаем её нулём, знак и порядок величины
    # сохраняются. Но если из формулы «покупки − продажи» нашлись ТОЛЬКО
    # продажи, ноль подставляется в базу, и CAPEX выезжает равным минус
    # выручке от продажи имущества. Это не «capex мал», это выдуманное число
    # с обратным знаком.
    #
    # Разметка: у Черкизово-2021 в отчёте нет строки покупок, продажи 323 и
    # 364, а размеченный CAPEX — 12 218 и 17 431. Формула дала бы −323/−364,
    # промах в сорок раз и мимо знака. Всего таких период-случаев в разметке
    # десять на пять документов (Черкизово, РЖД, Japan Airlines, Exxon, Intel);
    # в шести из них печатной строки CAPEX нет вовсе, то есть выдуманное
    # значение легло бы в пустую клетку и уехало в FREE_CASH_FLOW и FCFF.
    #
    # Правило: достраиваем нулём только тогда, когда хотя бы одно СЛАГАЕМОЕ
    # найдено. Нет ни одного — формулы для этого периода нет, клетка пустая.
    if present and (present & _added_formula_tokens(formula)):
        optional_zero_deps.update(tokens - present)
    return optional_zero_deps

def _formula_env(
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]] | None = None,
    *,
    optional_zero_deps: set[str] | None = None,
    formula_default_abs_enums: set[str] | None = None,
    calculated_outflow_enums: set[str] | None = None,
    abs_applied: set[str] | None = None,
    sign_flipped: set[str] | None = None,
) -> dict[str, float]:
    """Числа, по которым считает вычислитель.

    `abs_applied` — необязательный ВЫХОД: сюда попадают показатели, у которых
    знак отчёта был снят признаком `formula_default_abs_behavior`. Пометку
    собираем ЗДЕСЬ, а не второй проверкой снаружи: вторая реализация правила
    разошлась бы с первой, и подпись врала бы про свой же расчёт (Б5).

    `calculated_outflow_enums` — РАСЧЁТНЫЕ показатели, которым знак отчёта
    поставило правило `_result_is_outflow`. Здесь он снимается тем же
    отрицанием, каким был поставлен: в env лежит МОДУЛЬ потока, потому что
    формулы реестра написаны арифметикой модулей (`FREE_CASH_FLOW =
    NET_OPERATING_CASH_FLOW - CAPEX`). Правило и его снятие описаны одним
    куском в `_result_is_outflow`.

    `sign_flipped` — второй необязательный ВЫХОД, для подписи: у таких входов
    преобразование — отрицание, а не модуль, и назвать его `abs(...)` значило
    бы соврать там, где поток оказался входящим.
    """
    env: dict[str, float] = {}
    outflow = calculated_outflow_enums or set()
    for key, value in (calculated or {}).items():
        if value is None:
            continue
        try:
            numeric = float(value)
        except Exception:
            continue
        if str(key) in outflow:
            numeric = -numeric
            if abs_applied is not None:
                abs_applied.add(str(key))
            if sign_flipped is not None:
                sign_flipped.add(str(key))
        env[str(key)] = numeric
    default_abs = formula_default_abs_enums or set()
    for key, value in extracted.items():
        if value is None:
            continue
        try:
            numeric = float(value)
        except Exception:
            continue
        # Показатель, заведённый И извлекаемым, И расчётным, приходит сюда
        # дважды, и извлечённое значение перекрывает посчитанное. Пометку
        # расчётного тогда надо снять: она описывала бы уже не то число.
        if sign_flipped is not None:
            sign_flipped.discard(str(key))
        if str(key) in default_abs and numeric < 0:
            numeric = -numeric
            if abs_applied is not None:
                abs_applied.add(str(key))
        elif abs_applied is not None:
            abs_applied.discard(str(key))
        env[str(key)] = numeric
    for key in optional_zero_deps or set():
        env.setdefault(key, 0.0)
    return env

def _formula_inputs(
    expr: str,
    env: dict[str, float],
    zero_filled: set[str],
    abs_applied: set[str],
    avg_substitutions: list[dict],
    sign_flipped: set[str] | None = None,
) -> list[dict]:
    """Числа, реально подставленные в выражение: показатель → значение.

    ЦЕНА (Б5, 19.08.2026). В расчётной строке стоял ТОЛЬКО ТЕКСТ формулы —
    проверить результат читателю было не по чему, и два случая расхождения
    текста с расчётом жили молча: подстановка нуля вместо отсутствующего
    необязательного входа (`_formula_env`, ниже по файлу) и фолбэк EBITDA
    (`_calculate_metrics`).

    ЦЕНА ВТОРАЯ (20.08.2026, состязательная проверка). Первая редакция списка
    молчала ещё о двух преобразованиях, и подпись переставала сходиться с тем,
    что видит читатель:

      • `AVG(X)` раскрывается ДО разбора, и показателя из-под усреднения в
        `expr` уже нет. 69 формул из 359 — ROE, ROA, NIM, все оборачиваемости —
        получали список входов, из которого второе число просто пропало.
        Теперь среднее приходит отдельным входом `avg_substitutions`: его
        считает `_replace_avg_calls`, и число у него есть.
      • признак `formula_default_abs_behavior` (556 записей) снимает знак
        отчёта. Строка извлечения показывала −20, подпись расчёта +20, а
        `abs(...)` в формуле не стояло. Теперь такой вход помечен, и читатель
        видит, ЧТО с числом сделали.

    `transform` — что сделано со входом перед подстановкой: `""` (ничего),
    `"abs"` (снят знак), `"sign"` (знак сменён на обратный), `"avg"`
    (усреднение за период). Печатают его оба отрисовщика подписи — ролевая
    книга и таблица на экране, по общей разметке
    `tests/fixtures/calculation_signature.json`.

    ЗАЧЕМ ОТДЕЛЬНЫЙ `"sign"` РЯДОМ С `"abs"`. У расчётной строки знак отчёта
    ставится отрицанием и снимается отрицанием же. Совпадает с модулем оно
    только когда поток исходящий; когда входящий (банк получил процентов
    больше, чем уплатил) `abs(...)` в подписи не даёт напечатанного числа, и
    читатель ловит расхождение там, где его нет.
    """
    flipped = sign_flipped or set()
    inputs = [
        {
            "enum": token,
            "value": env[token],
            "substituted_zero": token in zero_filled,
            "transform": ("sign" if token in flipped
                          else "abs" if token in abs_applied else ""),
        }
        for token in sorted(_formula_metric_tokens(expr))
        if token in env
    ]
    named: set[str] = set()
    for item in avg_substitutions:
        inner = item["enum"]
        if inner in named:
            continue
        named.add(inner)
        inputs.append(
            {"enum": inner, "value": item["value"], "substituted_zero": False, "transform": "avg"}
        )
    return inputs


def _normalise_formula_expr(expr: str) -> str:
    normalised = (
        expr.replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("%", "")
    )
    return re.sub(r"(?<=\d),(?=\d)", ".", normalised)

class _FormulaError(ValueError):
    """Формула не разобралась или содержит конструкцию вне белого списка."""

# Формулы приходят из реестров (git), но реестр редактируемый, и рано или поздно
# формулу напишет пользователь или модель. Поэтому считаем не через eval с пустым
# `__builtins__` — такой «песочницы» не существует, `().__class__.__base__.__subclasses__()`
# пробивает её насквозь, — а разбираем в AST и ходим по белому списку узлов.
#
# ABS/MIN/MAX — единственные разрешённые вызовы. AVG сюда намеренно не входит:
# усреднение берёт значения из соседних периодов, а не из текущего env, поэтому
# его раскрывает `_replace_avg_calls` ДО разбора.
_FORMULA_FUNCS: dict[str, Callable[..., float]] = {"ABS": abs, "MIN": min, "MAX": max}

# ЧТО МОЖНО ЗВАТЬ В ФОРМУЛЕ — НЕ ТО ЖЕ, ЧТО СЧИТАЕТ ОБХОДЧИК AST (29.08.2026).
#
# `_FORMULA_FUNCS` — вызовы, которые исполняет `_eval_formula_node`. `AVG` и
# `ANNUAL` в него не входят намеренно: их раскрывают ДО разбора
# (`_replace_avg_calls` и приведение к году), и до обходчика они не доживают.
#
# ЦЕНА ПУТАНИЦЫ. Приёмный контроль своих формул (`ratios._refuse_unknown_calls`)
# сверялся с `_FORMULA_FUNCS` — то есть ДО раскрытия макросов и по списку,
# который про макросы ничего не знает. Формулы реестра с `AVG(...)` и
# `ANNUAL(EBITDA)` работали (они мимо этой проверки), а такая же формула,
# написанная моделью, отвергалась со словами «движок такого не знает». Тело
# навыка при этом само разрешало модели `ANNUAL` и давало пример с `AVG`:
# СВОЯ формула была слабее реестровой ровно на то, что ей обещали.
FORMULA_CALLABLES: frozenset[str] = frozenset(_FORMULA_FUNCS) | {"AVG", "ANNUAL"}

# Запятая внутри формулы — ДЕСЯТИЧНЫЙ разделитель: так написан реестр («* 0,75»
# в банковских ячейках). Неоднозначность возникает ровно в одном месте — внутри
# вызова с несколькими аргументами, где запятая ещё и разделяет аргументы:
# «MIN(NET_DEBT, 1,000)» читается и как «MIN от двух чисел», и как «MIN от трёх».
# Там — падаем с внятным текстом. Везде остальном запятая-десятичная законна, и
# запрещать её нельзя: первая версия этой проверки резала «* 0,125» и «* 0,075»,
# то есть ЛЕГАЛЬНУЮ дробь с тремя знаками (нашла независимая проверка).
_DECIMAL_COMMA_RE = re.compile(r"\d,\d")

_MULTIARG_CALL_RE = re.compile(r"\b(?:MIN|MAX)\s*\(")

def _parse_formula_expr(expr: str) -> ast.Expression:
    """Нормализовать и разобрать формулу РОВНО ОДИН раз.

    Раньше проверка (регулярка по токенам) и исполнение работали с разными
    строками — между ними стояла нормализация: проверяли одно, считали другое.
    Теперь дальше по коду идёт только это дерево.
    """
    if _MULTIARG_CALL_RE.search(expr) and _DECIMAL_COMMA_RE.search(expr):
        raise _FormulaError(
            "внутри MIN/MAX запятая неоднозначна (и десятичный разделитель, и разделитель "
            f"аргументов) — пишите дробь через точку: «{expr.strip()}»"
        )
    try:
        return ast.parse(_normalise_formula_expr(expr), mode="eval")
    except SyntaxError as exc:
        raise _FormulaError(f"не разобрать формулу «{expr.strip()}»: {exc.msg}") from exc

def _eval_formula_node(node: ast.AST, env: dict[str, float]) -> float:
    """Посчитать один узел разобранной формулы. Всё вне белого списка — ошибка."""
    if isinstance(node, ast.Expression):
        return _eval_formula_node(node.body, env)

    if isinstance(node, ast.BinOp):
        left = _eval_formula_node(node.left, env)
        right = _eval_formula_node(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right  # ZeroDivisionError наверх — как и раньше
        raise _FormulaError(f"оператор {type(node.op).__name__} не разрешён в формуле")

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval_formula_node(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return +_eval_formula_node(node.operand, env)
        raise _FormulaError(f"унарный {type(node.op).__name__} не разрешён в формуле")

    if isinstance(node, ast.Constant):
        # bool — подкласс int, но `True * 2` в финансовой формуле означает опечатку
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _FormulaError(f"в формуле разрешены только числа, получено {node.value!r}")
        return float(node.value)

    if isinstance(node, ast.Name):
        # Имя = метрика. Отсутствие входа — штатное «за этот период не посчитать»,
        # а не поломка формулы, поэтому здесь свой тип исключения.
        if node.id not in env:
            raise _MissingFormulaInput(node.id)
        return env[node.id]

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FORMULA_FUNCS:
            called = getattr(node.func, "id", type(node.func).__name__)
            raise _FormulaError(
                f"в формуле разрешены только вызовы {'/'.join(_FORMULA_FUNCS)}, получен «{called}»"
            )
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise _FormulaError(f"{node.func.id} принимает только позиционные аргументы")
        args = [_eval_formula_node(arg, env) for arg in node.args]
        if not args:
            raise _FormulaError(f"{node.func.id} вызван без аргументов")
        if node.func.id == "ABS" and len(args) != 1:
            raise _FormulaError("ABS принимает ровно один аргумент")
        return float(_FORMULA_FUNCS[node.func.id](*args))

    raise _FormulaError(f"конструкция {type(node).__name__} запрещена в формуле")

def _eval_formula_expr(expr: str, env: dict[str, float]) -> float:
    return float(_eval_formula_node(_parse_formula_expr(expr), env))

# ── смысл посчитанного: РАЗМЕРНОСТЬ ──────────────────────────────────────────
#
# ГРАБЛЯ (#146). Единицу рассчитанного показателя раньше угадывали по тексту
# формулы: «есть „* 100“ → проценты, иначе — деньги». Банковские коэффициенты
# написаны БЕЗ множителя (ROE = NET_INCOME / AVG(TOTAL_EQUITY)), поэтому ВСЕ они
# уезжали в промпт и в выгрузку с денежной подписью: «ROE: 0,246 млн. руб.».
# В одной и той же таблице ROA (с «* 100») шёл процентами, а ROE (без) —
# деньгами: два показателя с одним словом «рентабельность» отличались на глаз
# в сто раз.
#
# Теперь размерность ВЫВОДИТСЯ из структуры формулы: сложили метрики — деньги,
# поделили метрику на метрику — безразмерная величина. Реестр может перебить
# вывод полем `unit_kind` — там, где структура врёт (см. `unit_for_calculated`).
_DIM_MONEY = "money"

_DIM_RATIO = "ratio"

_DIM_SCALAR = "scalar"  # голая константа (365, 0,75) своей размерности не несёт

# ABS/MIN/MAX/AVG/ANNUAL размерность аргумента не меняют. AVG и ANNUAL в белый
# список ВЫЧИСЛИТЕЛЯ не входят (их раскрывают до разбора), но здесь мы смотрим
# на ИСХОДНУЮ формулу из реестра, где они ещё на месте.
_DIM_TRANSPARENT_FUNCS = {"ABS", "MIN", "MAX", "AVG", "ANNUAL"}

def _node_dimension(node: ast.AST) -> str:
    if isinstance(node, ast.Expression):
        return _node_dimension(node.body)
    if isinstance(node, ast.Constant):
        return _DIM_SCALAR
    if isinstance(node, ast.Name):
        # `DAYS_IN_PERIOD` — те же 365, только названные: счёт дней, а не деньги.
        if node.id == "DAYS_IN_PERIOD":
            return _DIM_SCALAR
        return _DIM_MONEY  # имя метрики = величина из отчёта
    if isinstance(node, ast.UnaryOp):
        return _node_dimension(node.operand)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _DIM_TRANSPARENT_FUNCS:
            return _DIM_MONEY  # незнакомый вызов — вычислитель его всё равно отвергнет
        dims = [_node_dimension(a) for a in node.args]
        return next((d for d in dims if d != _DIM_SCALAR), _DIM_SCALAR)
    if isinstance(node, ast.BinOp):
        left, right = _node_dimension(node.left), _node_dimension(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            # Складывать разное нельзя, поэтому берём ту сторону, что не константа.
            return left if left != _DIM_SCALAR else right
        if isinstance(node.op, ast.Mult):
            # «* 100», «* 0,75» — масштаб, размерность второй стороны сохраняется.
            if right == _DIM_SCALAR:
                return left
            if left == _DIM_SCALAR:
                return right
            return _DIM_MONEY
        if isinstance(node.op, ast.Div):
            if right == _DIM_SCALAR:
                return left
            return _DIM_RATIO  # деньги÷деньги и 365÷(поток÷остаток) — безразмерно
    return _DIM_MONEY

def formula_dimension(formula: str) -> str:
    """`money` | `ratio` | `scalar` — что за величина получится по формуле.

    Формула из реестра редактируема, и синтаксическая ошибка в ней не должна
    ронять весь прогон на подписи единицы: не разобралось — считаем деньгами,
    то есть ведём себя как до появления этой проверки.
    """
    try:
        return _node_dimension(_parse_formula_expr(formula or ""))
    except (_FormulaError, RecursionError):
        return _DIM_MONEY

# Что реестр может сказать про единицу словом (`unit_kind`), когда структура
# формулы врёт. Значение None = «деньги отчёта», то есть валюта прогона: писать
# «млн. руб.» в реестр нельзя — тот же показатель считается и по us_gaap в
# долларах.
_UNIT_BY_KIND: dict[str, str | None] = {
    "money": None,
    "percent": "%",
    "ratio": "",
    "days": "дн.",
}

def unit_for_calculated(formula: str, default_unit: str, unit_kind: str = "") -> str:
    """Подпись единицы для рассчитанного показателя.

    Порядок: явное слово реестра → «* 100» в формуле → вывод по структуре.
    Безразмерная величина остаётся БЕЗ подписи: пустая подпись честнее ложной
    денежной — читатель видит «0,24», а не «0,24 млн. руб.».
    """
    kind = str(unit_kind or "").strip().lower()
    if kind in _UNIT_BY_KIND:
        resolved = _UNIT_BY_KIND[kind]
        return default_unit if resolved is None else resolved
    if "* 100" in formula or "× 100" in formula:
        return "%"
    return "" if formula_dimension(formula) == _DIM_RATIO else default_unit

# ── смысл посчитанного: ПРИВЕДЕНИЕ К ГОДУ ────────────────────────────────────
#
# ГРАБЛЯ (#146). Методика анализа называет NIM, стоимость риска и рентабельности
# АННУАЛИЗИРОВАННЫМИ («NIM = аннуализ. NII / средние активы»), а в коде
# приведения к году не было нигде. На полугодовом отчёте банк выходил ровно
# вдвое хуже, чем он есть, и промпт при этом утверждал обратное.
#
# Приводим ПРОСТЫМ множителем 12/N месяцев — самая ходовая конвенция и
# единственная, которой хватает данных одного отчёта (LTM требует второго).
# Способ приведения — часть определения показателя, поэтому он назван в тексте
# методики (`bank.formula.*`), а не спрятан здесь.
_MONTHS_IN_PERIOD_RE = re.compile(r"^(\d{1,2})M\b")

def _period_months(period_label: object) -> int | None:
    """Сколько месяцев покрывает период, если это вообще известно из метки.

    Нераспознанная метка и балансовая дата («31.03.2024») длины периода не
    несут — None. Разбор метки живёт ОДИН раз: и множитель приведения, и его
    подпись читателю берутся отсюда, иначе они разошлись бы (Б5).
    """
    label = normalize_period_label(period_label)
    if re.fullmatch(r"FY 20\d{2}", label):
        return 12
    if re.fullmatch(r"Q[1-4] 20\d{2}", label):
        return 3
    m = _MONTHS_IN_PERIOD_RE.match(label)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    return None

def annualization_factor(period_label: object) -> float:
    """Во сколько раз умножить величину ЗА ПЕРИОД, чтобы получить годовую."""
    months = _period_months(period_label)
    return 1.0 if months is None else 12.0 / months

def annualization_note(period_label: object) -> str:
    """Множитель приведения к году так, как его проверит читатель: «12/9».

    Дробью, а не числом: 12/9 — это 1,3333…, и напечатанное округление
    не даёт напечатанного результата. Пустая строка = приведения не было.
    """
    months = _period_months(period_label)
    return "" if months is None or months == 12 else f"12/{months}"

# ── смысл посчитанного: ЗАПАС, ПОДЕЛЁННЫЙ НА ПОТОК ───────────────────────────
#
# ГРАБЛЯ (порт 1d3e005). Приведение к году выше лечит показатели, где ПОТОК
# стоит в ЧИСЛИТЕЛЕ (ROE, NIM): их поднимает признак `annualize` при выдаче.
# Обратный случай — запас в числителе, поток в знаменателе — не лечил никто:
# «Чистый долг / EBITDA» на полугодовом отчёте делил долг на дату на половину
# годовой EBITDA и выходил ВДВОЕ ХУЖЕ настоящего, а оборачиваемости делили
# зашитые 365 дней на полугодовой поток и выходили вдвое длиннее. 7,8x долговой
# нагрузки вместо 3,9x — это первое число, на которое смотрит кредитный
# читатель, и оно было неправдой.
#
# Длину периода теперь называет САМА ФОРМУЛА, а не прячет движок:
# `DAYS_IN_PERIOD` — сколько дней покрывает отчёт, `ANNUAL(X)` — поток X,
# поднятый до двенадцати месяцев. Годовой отчёт не двигается по построению:
# там `DAYS_IN_PERIOD` = 365, а `ANNUAL()` — тождество.
DAYS_IN_YEAR = 365

def days_in_period(period_label: object) -> int:
    """Сколько дней покрывает период. Неизвестная метка и балансовая дата — год.

    Дата своей длительности не несёт, а формула, где остаток на дату стоит
    рядом с потоком, длину берёт у потока — поэтому год честен и здесь, и для
    `FY`/`12M`. Месяцы считает `_period_months`: разбор метки живёт один раз.
    """
    months = _period_months(period_label)
    return DAYS_IN_YEAR if months is None else round(DAYS_IN_YEAR * months / 12)

# Ровно те выражения, которыми `formula_for_period` НАХОДИТ длину периода в
# формуле. Названы один раз и наружу, потому что читателя у правила два: сам
# движок и проверка реестра (`CalcDef`), запрещающая приведение к году дважды.
# Порознь они уже разошлись — проверка искала подстроку «ANNUAL(» в формуле без
# пробелов и врала в обе стороны: пропускала `ANNUAL\t(EBITDA)`, которую движок
# раскрывает (×16 на квартале), и отвергала `SEMIANNUAL(...)` и
# `MY_DAYS_IN_PERIOD`, которых движок не трогает.
_PERIOD_LENGTH_RE = re.compile(r"\bANNUAL\s*\(|\bDAYS_IN_PERIOD\b")

def formula_carries_period_length(formula: object) -> bool:
    """Формула сама поднимает величину до года — `ANNUAL(...)`/`DAYS_IN_PERIOD`."""
    return bool(_PERIOD_LENGTH_RE.search(str(formula or "")))

def formula_for_period(formula: str, period_label: object) -> str:
    """Формула, в которой длина периода названа ЧИСЛОМ, а не именем.

    Раскрываем ДО разбора — тем же порядком, что и `AVG(...)`, и по той же
    причине: значение приходит не из env текущего периода.

    Раскрытая строка едет и в вычислитель, и в подпись, и это не удобство, а
    требование (Б5): читатель, которому показали `NET_DEBT / ANNUAL(EBITDA)` и
    два входа под ней, поделит вход на вход и получит ВДВОЕ БОЛЬШЕЕ число, чем
    напечатано в строке. Множитель пишем дробью — «12/6», а не «2,0»: у 9М это
    1,3333…, и напечатанное округление не даёт напечатанного результата.

    АРГУМЕНТ БЕРЁТСЯ В СВОИ СКОБКИ, и это не украшение. Множитель приписывается
    СТРОКОЙ, а строку потом читает разборщик по правилам приоритета: без своих
    скобок `ANNUAL(EBITDA - ONE_OFF)` раскрывается в `EBITDA - ONE_OFF * 12/6`,
    то есть к году поднимается ОДНО ПОСЛЕДНЕЕ слагаемое, а не весь поток. На
    полугодии это даёт 10 вместо 2,5 — и подпись такую ошибку не ловит, потому
    что сходится сама с собой. Сегодня у всех шести записей реестра аргумент —
    одно имя, но реестр правится текстом и без программиста: первый же
    составной аргумент, написанный аналитиком, попал бы в эту яму молча.
    """
    if not formula_carries_period_length(formula):
        return formula

    months = _period_months(period_label)
    expanded = re.sub(r"\bDAYS_IN_PERIOD\b", str(days_in_period(period_label)), formula)

    result: list[str] = []
    pos = 0
    while pos < len(expanded):
        match = re.search(r"\bANNUAL\s*\(", expanded[pos:])
        if not match:
            result.append(expanded[pos:])
            break
        start = pos + match.start()
        open_paren = pos + match.end() - 1
        result.append(expanded[pos:start])
        depth = 1
        end = open_paren + 1
        while end < len(expanded) and depth:
            if expanded[end] == "(":
                depth += 1
            elif expanded[end] == ")":
                depth -= 1
            end += 1
        if depth:
            raise _FormulaError(f"у ANNUAL не закрыта скобка: «{expanded.strip()}»")
        inner = expanded[open_paren + 1 : end - 1].strip()
        result.append(f"({inner})" if months in (None, 12) else f"(({inner}) * 12/{months})")
        pos = end
    return "".join(result)

def _sanity_note(enum: object, value: object, unit: str, calculated_meta: dict) -> str:
    """Пометка «получилось бессмысленное» рядом со значением — и в лог.

    Молчать нельзя: перевёрнутый Cost/Income (#146) доехал до пользователя
    именно потому, что движок печатал 4,1 с тем же спокойствием, что и 0,24.
    Пустая строка = претензий нет (в событии остаётся пустое поле, потребителю
    не надо знать про None).
    """
    key = str(enum or "").strip().upper()
    bounds = (calculated_meta or {}).get(key) or {}
    low = bounds.get("soft_min_value")
    high = bounds.get("soft_max_value")
    if value is None or (low is None and high is None):
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    scale = 100.0 if str(unit or "").strip() == "%" else 1.0
    low_scaled = None if low is None else float(low) * scale
    high_scaled = None if high is None else float(high) * scale
    outside = (
        (low_scaled is not None and numeric < low_scaled)
        or (high_scaled is not None and numeric > high_scaled)
    )
    if not outside:
        return ""
    suffix = "%" if scale == 100.0 else ""
    interval = f"[{low_scaled!r}, {high_scaled!r}]"
    note = f"вне диапазона {interval}{suffix}: получено {numeric!r}{suffix}"
    _LOG.warning("расчёт вне диапазона смысла: %s — %s", enum, note)
    return note

def annualized(value: object, period_label: object, annualize: bool) -> Optional[float]:
    """Значение, приведённое к году, если показатель это требует.

    Приведение делается ПРИ ВЫДАЧЕ, а не внутри вычислителя: формулы-потребители
    (напр. операционный доход через CIR) написаны против величины за период, и
    аннуализированный вход сломал бы их.
    """
    if value is None or not annualize:
        return value if value is None else float(value)
    return float(value) * annualization_factor(period_label)

def _with_point_values_for_duration_calculations(
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    periods: list[str],
) -> dict[str, dict[str, Optional[float]]]:
    """Expose point-in-time values to duration formulas with the same end date.

    This is intentionally calculation-only. The original period_values stay on
    their source date so UI/export code can decide a single display column.
    """
    out = {period: dict(values) for period, values in extracted_by_period.items()}
    point_values: list[tuple[tuple[int, int, int], str, Optional[float]]] = []
    for period, values in extracted_by_period.items():
        point_key = _point_period_key(period)
        if point_key is None:
            continue
        for enum, value in values.items():
            if period_kind_for_enum(enum) == POINT_KIND:
                point_values.append((point_key, enum, value))

    if not point_values:
        return out

    for period in periods:
        if _point_period_key(period) is not None:
            continue
        end_key = _period_end_key(period)
        if end_key is None:
            continue
        target = out.setdefault(period, {})
        for point_key, enum, value in point_values:
            if point_key == end_key:
                target.setdefault(enum, value)
    return out

def _calculated_period_kind_map(calc_defs: list) -> dict[str, str]:
    """Infer whether calculated metrics are point-like or duration-like."""
    calc_by_enum = {str(enum).strip().upper(): (formula, deps) for enum, _name, formula, deps in calc_defs}
    memo: dict[str, str] = {}
    visiting: set[str] = set()

    def kind_for(enum: object) -> str:
        key = str(enum or "").strip().upper()
        if not key:
            return DURATION_KIND
        if key in memo:
            return memo[key]
        if key not in calc_by_enum:
            return period_kind_for_enum(key)
        if key in visiting:
            return DURATION_KIND

        visiting.add(key)
        _formula, deps = calc_by_enum[key]
        dep_kinds = [
            kind_for(dep)
            for dep in deps
            if str(dep or "").strip().upper() not in _NON_METRIC_DEPS
        ]
        visiting.remove(key)
        memo[key] = POINT_KIND if dep_kinds and all(dep_kind == POINT_KIND for dep_kind in dep_kinds) else DURATION_KIND
        return memo[key]

    for enum in calc_by_enum:
        kind_for(enum)
    return memo

def _env_from_period_values(
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    calculated_by_period: dict[str, dict[str, Optional[float]]],
    period: str,
    formula_default_abs_enums: set[str] | None = None,
    calculated_outflow_by_period: dict[str, set[str]] | None = None,
) -> dict[str, float]:
    """Env для `AVG(...)` — ВТОРАЯ сборка окружения, и правила в ней те же.

    ⚠️ Снятие знака у расчётного оттока обязано стоять И ЗДЕСЬ: путь `AVG()`
    берёт числа не через `_formula_env`, и правило, заведённое в одном месте,
    на этом пути не сработало бы.
    """
    env: dict[str, float] = {}
    outflow = (calculated_outflow_by_period or {}).get(period) or set()
    for key, value in calculated_by_period.get(period, {}).items():
        if value is None:
            continue
        try:
            numeric = float(value)
        except Exception:
            continue
        env[str(key)] = -numeric if str(key) in outflow else numeric
    default_abs = formula_default_abs_enums or set()
    for key, value in extracted_by_period.get(period, {}).items():
        if value is None:
            continue
        try:
            numeric = float(value)
        except Exception:
            continue
        if str(key) in default_abs:
            numeric = abs(numeric)
        env[str(key)] = numeric
    return env

def _build_avg_resolver_for_period(
    target_period: str,
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    calculated_by_period: dict[str, dict[str, Optional[float]]],
    point_periods: list[str],
    formula_default_abs_enums: set[str] | None = None,
    period_calendar: str = "calendar",
    calculated_outflow_by_period: dict[str, set[str]] | None = None,
) -> Callable[[str], float] | None:
    target_key = _period_end_key(target_period, period_calendar)
    if target_key is None:
        return None
    ordered_points = sorted(
        ((p, _point_period_key(p)) for p in point_periods),
        key=lambda item: item[1] or (0, 0, 0),
    )
    if not ordered_points:
        return None

    def resolve(inner_expr: str) -> float:
        boundary_keys = _period_boundary_keys(target_period, period_calendar)
        if boundary_keys is not None:
            values: list[float] = []
            for boundary_key in boundary_keys:
                point_period = next(
                    (
                        p
                        for p, point_key in ordered_points
                        if point_key == boundary_key
                    ),
                    None,
                )
                if point_period is None:
                    raise _MissingFormulaInput("AVG boundary point periods")
                env = _env_from_period_values(
                    extracted_by_period,
                    calculated_by_period,
                    point_period,
                    formula_default_abs_enums,
                    calculated_outflow_by_period,
                )
                values.append(_eval_formula_expr(inner_expr, env))
            return (values[0] + values[1]) / 2.0

        values: list[float] = []
        for point_period, point_key in ordered_points:
            if point_key is None or point_key > target_key:
                continue
            env = _env_from_period_values(
                extracted_by_period,
                calculated_by_period,
                point_period,
                formula_default_abs_enums,
                calculated_outflow_by_period,
            )
            try:
                values.append(_eval_formula_expr(inner_expr, env))
            except Exception:
                continue
        if len(values) < 2:
            raise _MissingFormulaInput("AVG point periods")
        current_value = values[-1]
        previous_value = values[-2]
        return (current_value + previous_value) / 2.0

    return resolve

def _replace_avg_calls(
    formula: str,
    current_env: dict[str, float],
    previous_env: dict[str, float] | None,
    avg_resolver: Callable[[str], float] | None = None,
    substitutions: list[dict] | None = None,
) -> str:
    """Replace only scoped AVG(...) calls with their current/previous average.

    `substitutions` — необязательный ВЫХОД для подписи строки: усреднённое
    число известно ровно здесь и больше нигде (Б5). Без него список входов
    молчал про второе слагаемое каждой формулы с `AVG(...)`.
    """
    result: list[str] = []
    pos = 0
    while pos < len(formula):
        match = re.search(r"\bAVG\s*\(", formula[pos:], re.I)
        if not match:
            result.append(formula[pos:])
            break

        start = pos + match.start()
        open_paren = pos + match.end() - 1
        result.append(formula[pos:start])

        depth = 1
        end = open_paren + 1
        while end < len(formula) and depth:
            if formula[end] == "(":
                depth += 1
            elif formula[end] == ")":
                depth -= 1
            end += 1
        inner = formula[open_paren + 1 : end - 1].strip()
        if depth:
            raise _MissingFormulaInput("AVG closing parenthesis")
        if avg_resolver is not None:
            average = avg_resolver(inner)
        else:
            if previous_env is None:
                raise _MissingFormulaInput("AVG previous period")
            current_value = _eval_formula_expr(inner, current_env)
            previous_value = _eval_formula_expr(inner, previous_env)
            average = (current_value + previous_value) / 2.0
        result.append(str(average))
        if substitutions is not None:
            substitutions.append({"enum": inner, "value": average})
        pos = end
    return "".join(result)

def _eval_calculated_formula(
    formula: str,
    extracted: dict[str, Optional[float]],
    calculated: dict[str, Optional[float]],
    previous_extracted: dict[str, Optional[float]] | None,
    *,
    previous_calculated: dict[str, Optional[float]] | None = None,
    optional_zero_deps: set[str] | None = None,
    avg_resolver: Callable[[str], float] | None = None,
    formula_default_abs_enums: set[str] | None = None,
    outflow_enums: set[str] | None = None,
    previous_outflow_enums: set[str] | None = None,
    enum: str = "",
    trace: dict | None = None,
    period: str = "",
) -> float:
    """Одна расчётная строка: число и (по запросу) её происхождение.

    `outflow_enums` — набор расчётных показателей, которым правило
    `_result_is_outflow` поставило знак отчёта. Параметр работает В ОБЕ
    СТОРОНЫ: на входе он говорит, у кого из уже посчитанных снять знак перед
    подстановкой, на выходе сюда дописывается `enum`, если правило сработало
    на этой строке. Один набор, а не два, потому что это одно и то же
    множество в разные моменты одного прохода, и разведя их, мы завели бы
    вторую его копию, способную разойтись с первой.
    """
    abs_applied: set[str] = set()
    sign_flipped: set[str] = set()
    current_env = _formula_env(
        extracted,
        calculated,
        optional_zero_deps=optional_zero_deps,
        formula_default_abs_enums=formula_default_abs_enums,
        calculated_outflow_enums=outflow_enums,
        abs_applied=abs_applied,
        sign_flipped=sign_flipped,
    )
    # Пометки сравнительного периода не собираем: в подпись строки едет
    # происхождение ОСНОВНОГО периода, его числа читатель и проверяет.
    previous_env = (
        _formula_env(
            previous_extracted or {},
            previous_calculated,
            optional_zero_deps=optional_zero_deps,
            formula_default_abs_enums=formula_default_abs_enums,
            calculated_outflow_enums=previous_outflow_enums,
        )
        if previous_extracted is not None or previous_calculated is not None
        else None
    )

    dated = formula_for_period(formula, period)
    avg_substitutions: list[dict] = []
    expr = _replace_avg_calls(
        dated, current_env, previous_env, avg_resolver, avg_substitutions
    )
    value = _eval_formula_expr(expr, current_env)
    outflow = _result_is_outflow(expr, current_env, abs_applied)
    if outflow:
        # `-0.0` — тот же ноль, но ДВА МЕСТА печатают его по-разному: книга
        # роли даёт «0», `toLocaleString('ru-RU')` на экране — «-0». Одна
        # клетка, два ответа; гасим отрицательный ноль здесь, у источника.
        value = -value if value else 0.0
        if enum and outflow_enums is not None:
            outflow_enums.add(enum)
    if trace is not None:
        # Достроенные нулём входы вычисляем ТЕМ ЖЕ предикатом, которым
        # `_formula_env` решает, что класть в env: вторая реализация правила
        # подстановки разошлась бы с первой, и пометка врала бы (Б5).
        zero_filled = set(optional_zero_deps or ()) - _numeric_env_keys(extracted, calculated)
        # Подписываем РАСКРЫТОЙ формулой: `ANNUAL(EBITDA)` читателю не сходится,
        # `(EBITDA * 12/6)` сходится (Б5). У 332 расчётных строк реестра из 361
        # новых имён нет вовсе, и раскрытие возвращает ту же строку.
        # Подпись обязана ВОСПРОИЗВОДИТЬ число строки (Б5). Знак оттока снят с
        # результата, а не с входов, поэтому он стоит и в подписи: `-(...)`
        # читается по тем же входам и даёт ровно то, что в клетке.
        trace["formula"] = f"-({dated})" if outflow else dated
        trace["inputs"] = _formula_inputs(
            expr, current_env, zero_filled, abs_applied, avg_substitutions, sign_flipped
        )
    return value


def _apply_ebitda_bottom_up_fallback(
    extracted: dict[str, Optional[float]],
    result: dict[str, Optional[float]],
    trace: dict[str, dict] | None,
) -> None:
    """Реестр определяет EBITDA как OPERATING_PROFIT + DEPRECIATION_AMORTIZATION.

    На Лукойле 2021 (run-3 в нашей валидации) экстрактор пропустил
    OPERATING_PROFIT — реранкер не вытащил стр. 9 ОПУ — и в результате EBITDA
    не считалась, а зависимые цепочки 1, 2, 19 говорили «данных нет». При этом
    NET_INCOME, EBT, INTEREST_PAID и D&A были извлечены. Алгебраически
    EBITDA = EBT + |INTEREST_PAID| + D&A — bottom-up через ОПУ, корректный
    fallback. Зовётся из `_calculate_metrics` только когда основная (реестровая)
    формула не дала результата для узла EBITDA.
    """
    ebt = extracted.get("EBT")
    # Показатель процентов назван ОДИН РАЗ и переиспользован: подпись фолбэка
    # обязана назвать тот вход, который в него реально пошёл, а `X or Y`
    # разводит выбор и его имя по разным строкам (Б5).
    interest_enum = "INTEREST_PAID" if extracted.get("INTEREST_PAID") else "FINANCIAL_EXPENSES"
    interest = extracted.get(interest_enum)
    depam = extracted.get("DEPRECIATION_AMORTIZATION")
    if ebt is None or interest is None or depam is None:
        return
    try:
        result["EBITDA"] = float(ebt) + abs(float(interest)) + float(depam)
    except Exception:
        return
    # 🔴 ПОДПИСЬ ОБЯЗАНА СОВПАСТЬ С РАСЧЁТОМ (Б5, 19.08.2026). До этой правки в
    # строку уезжала РЕЕСТРОВАЯ формула `OPERATING_PROFIT +
    # DEPRECIATION_AMORTIZATION`, а число считалось снизу вверх через EBT —
    # читатель проверял результат по формуле, которая его не даёт, и
    # расхождение выглядело ошибкой извлечения.
    if trace is not None and result.get("EBITDA") is not None:
        trace["EBITDA"] = {
            "formula": f"EBT + abs({interest_enum}) + DEPRECIATION_AMORTIZATION",
            "inputs": [
                {"enum": "EBT", "value": float(ebt),
                 "substituted_zero": False, "transform": ""},
                # Знак процентов НЕ снимаем: `abs(...)` стоит в самой подписи
                # выше, и число во входе обязано совпасть со строкой
                # извлечения, из которой читатель его сверяет.
                {"enum": interest_enum, "value": float(interest),
                 "substituted_zero": False, "transform": ""},
                {"enum": "DEPRECIATION_AMORTIZATION", "value": float(depam),
                 "substituted_zero": False, "transform": ""},
            ],
        }


def _calculate_metrics(
    calc_defs: list,
    extracted: dict[str, Optional[float]],
    previous_extracted: dict[str, Optional[float]] | None = None,
    avg_resolver: Callable[[str], float] | None = None,
    *,
    previous_calculated: dict[str, Optional[float]] | None = None,
    formula_default_abs_enums: set[str] | None = None,
    outflow_enums: set[str] | None = None,
    previous_outflow_enums: set[str] | None = None,
    trace: dict[str, dict] | None = None,
    period: str = "",
) -> dict[str, Optional[float]]:
    """Eval calculated formulas from the registry against extracted spot values.

    `trace` — необязательный ВТОРОЙ КАНАЛ (Б5, 19.08.2026): по нему уезжает
    происхождение каждого посчитанного показателя — `{enum: {"formula",
    "inputs"}}`. Отдельным каналом, а не в возвращаемом словаре, потому что
    этот словарь читают три производителя строк и тесты паритета методики:
    сменить его тип значило бы сломать всех разом.

    `period` — метка периода, за который сняты входы (Б5, 20.08.2026). Без неё
    подпись врала в широкой книге: колонок у строки три и больше, входы сняты
    за ОДНУ, а подпись стоит одна на всю строку — читатель, сверявший соседнюю
    колонку, получал числа, которые её не дают. Ставим её ЗДЕСЬ, у самих
    входов: производителей строк пять, и назвать период в каждом значило бы
    завести пятую возможность забыть. Она же даёт формулам ДЛИНУ периода
    (`DAYS_IN_PERIOD`, `ANNUAL()`); пустая метка читается как год, то есть
    считает ровно как до появления этих имён.

    Two non-trivial pre-processing steps before eval, fixing legacy formula
    syntax that doesn't parse as Python:

    1. `* 100%` → `* 100`. The trailing `%` is editorial («процент»), not the
       Python modulo operator. Without this strip every *_MARGIN formula
       (EBITDA_MARGIN, NET_INCOME_MARGIN, OPERATING_MARGIN, ROA с `%`, …)
       throws SyntaxError silently and returns None. На Лукойле 2021 это
       проявлялось как «маржа по EBITDA не посчитана» в цепочках 1, 17.

    2. `AVG(X)` is evaluated from point-in-time date rows when period_values are
       available: evaluate X on the latest date at or before the target period
       end and on the previous date where X is computable. Legacy current/prev
       callers without date rows keep the old two-env fallback.
    """
    # 🔴 ЗНАК ОТЧЁТА У РАСЧЁТНОЙ СТРОКИ — ЭТО КОНВЕНЦИЯ ЭКРАНА, НЕ ПОДСТАНОВКИ.
    #
    # В `result` лежит то, что увидит читатель: у оттока — минус (правило
    # `_result_is_outflow`). Но формулы реестра писаны арифметикой МОДУЛЕЙ, и
    # тот же `result` едет во входы следующих формул. Набор ниже — список
    # показателей, которым знак поставило правило; по нему `_formula_env`
    # снимает его обратно перед подстановкой.
    #
    # ЦЕНА (состязательная проверка 21.08.2026). Без этого набора починка
    # CAPEX (+43 591 → −43 591, верно) ломала КАЖДОГО его потребителя:
    # `FREE_CASH_FLOW = NET_OPERATING_CASH_FLOW - CAPEX` считал
    # `60000 − (−43591)` и печатал 103 591 — свободный поток БОЛЬШЕ
    # операционного при капзатратах 43,6 млрд. Подпись строки при этом
    # внутренне сходилась, и читатель воспроизвёл бы неверное число.
    outflow_enums = set() if outflow_enums is None else outflow_enums
    result: dict[str, Optional[float]] = {}

    # ── Порядок вычисления — топологический, не фиксированное число проходов ──
    # 🔴 БЫЛО (до этого коммита): `for _ in range(3)` — три жёстко зашитых
    # прохода в ПОРЯДКЕ РЕЕСТРА, а не порядке зависимостей, плюс ручная
    # заплатка-«четвёртый проход» специально для EBITDA-зависимых метрик
    # (EBITDA_MARGIN, RATIO_NET_DEBT_EBITDA, RATIO_TOTAL_DEBT_EBITDA) —
    # доказательство, что трёх проходов однажды не хватило. Число проходов
    # ничем не гарантировано относительно реальной глубины цепочки
    # зависимостей: цепочка глубже — тихо теряла бы верхние уровни (см.
    # tests/test_calc_defs_order_bounds_the_multipass_depth.py — характеризация
    # именно этого дефекта на синтетической цепочке глубины 4).
    #
    # СТАЛО: граф зависимостей строится из уже существующего поля `deps`
    # каждой формулы (ребро — только на ДРУГУЮ расчётную метрику; сырое
    # извлечение уже целиком в `extracted` и ребро не создаёт) и обходится
    # топологической сортировкой Кана — каждая формула считается РОВНО один
    # раз, в порядке, где все её расчётные зависимости уже посчитаны, какой бы
    # глубины ни была цепочка.
    calc_by_enum = {enum: (name, formula, deps) for enum, name, formula, deps in calc_defs}
    зависимые_от: dict[str, list[str]] = {enum: [] for enum in calc_by_enum}
    непосчитанных_входов: dict[str, int] = {enum: 0 for enum in calc_by_enum}
    for enum, (_name, _formula, deps) in calc_by_enum.items():
        for dep in deps:
            if dep in calc_by_enum and dep != enum:
                зависимые_от[dep].append(enum)
                непосчитанных_входов[enum] += 1

    очередь = deque(enum for enum, n in непосчитанных_входов.items() if n == 0)
    посчитано: set[str] = set()
    while очередь:
        enum = очередь.popleft()
        _name, formula, _deps = calc_by_enum[enum]
        optional_zero_deps = _optional_zero_deps_for_formula(enum, formula, extracted, result)
        attempt: dict | None = {} if trace is not None else None
        try:
            result[enum] = _eval_calculated_formula(
                formula,
                extracted,
                result,
                previous_extracted,
                previous_calculated=previous_calculated,
                optional_zero_deps=optional_zero_deps,
                avg_resolver=avg_resolver,
                formula_default_abs_enums=formula_default_abs_enums,
                outflow_enums=outflow_enums,
                previous_outflow_enums=previous_outflow_enums,
                enum=enum,
                trace=attempt,
                period=period,
            )
        except Exception:
            result[enum] = None
        if attempt and result.get(enum) is not None:
            trace[enum] = attempt

        # Точечный фолбэк для EBITDA — на месте её узла в обходе, а не отдельным
        # постфактум-проходом (см. `_apply_ebitda_bottom_up_fallback` выше по
        # файлу): раз обход топологический, любой зависимый от EBITDA узел
        # (прямой или транзитивный — EBITDA_MARGIN, а через неё что угодно ещё)
        # обработается ПОЗЖЕ и увидит уже готовое значение, включая фолбэк.
        if enum == "EBITDA" and result.get("EBITDA") is None:
            _apply_ebitda_bottom_up_fallback(extracted, result, trace)

        посчитано.add(enum)
        for следующий in зависимые_от[enum]:
            непосчитанных_входов[следующий] -= 1
            if непосчитанных_входов[следующий] == 0:
                очередь.append(следующий)

    # Узлы, не дождавшиеся нуля во входах, — участники цикла зависимостей или
    # его транзитивные потребители. Реестр такого сегодня не содержит (проверено
    # обходом всех calc_defs), но формула — текст, который правит не код, и
    # тихий None без объяснения — ровно тот дефект, который эта миграция снимает
    # у фиксированных проходов. Явно называем находку, а не молчим.
    циклические = calc_by_enum.keys() - посчитано
    if циклические:
        _LOG.warning(
            "formula_engine: циклическая зависимость в расчётных формулах, "
            "не посчитаны: %s", sorted(циклические),
        )
        for enum in циклические:
            result.setdefault(enum, None)

    if trace is not None and period:
        for entry in trace.values():
            for item in entry.get("inputs") or ():
                item["period"] = period

    return result

def _calculate_metrics_for_periods(
    calc_defs: list,
    extracted_by_period: dict[str, dict[str, Optional[float]]],
    periods: list[str],
    formula_default_abs_enums: set[str] | None = None,
    period_calendar: str = "calendar",
    trace_by_period: dict[str, dict] | None = None,
) -> dict[str, dict[str, Optional[float]]]:
    """Calculate metrics per period, resolving AVG from point-in-time rows.

    ``sort_periods`` returns newest → oldest, so we iterate oldest → newest.
    That lets calculated point metrics exist before later duration metrics call
    `AVG(CALCULATED_POINT_METRIC)`.
    """
    ordered = sort_periods(periods)
    result_by_period: dict[str, dict[str, Optional[float]]] = {}
    # Знак отчёта у расчётной строки — конвенция экрана; чтобы соседний период
    # подставлял модуль, набор помеченных надо помнить ПО ПЕРИОДАМ. Разбор — в
    # `_calculate_metrics`.
    outflow_by_period: dict[str, set[str]] = {}
    point_periods = sorted(
        {p for p in extracted_by_period if _point_period_key(p)},
        key=period_sort_key,
    )
    for idx in range(len(ordered) - 1, -1, -1):
        period = ordered[idx]
        previous_period = ordered[idx + 1] if idx + 1 < len(ordered) else None
        previous_extracted = None
        previous_calculated = None
        previous_outflow = None
        if previous_period is not None:
            previous_extracted = extracted_by_period.get(previous_period, {})
            previous_calculated = result_by_period.get(previous_period, {})
            previous_outflow = outflow_by_period.get(previous_period)
        avg_resolver = _build_avg_resolver_for_period(
            period,
            extracted_by_period,
            result_by_period,
            point_periods,
            formula_default_abs_enums,
            period_calendar=period_calendar,
            calculated_outflow_by_period=outflow_by_period,
        )
        period_trace: dict[str, dict] | None = (
            trace_by_period.setdefault(period, {}) if trace_by_period is not None else None
        )
        outflow_by_period[period] = set()
        result_by_period[period] = _calculate_metrics(
            calc_defs,
            extracted_by_period.get(period, {}),
            previous_extracted,
            avg_resolver,
            previous_calculated=previous_calculated,
            formula_default_abs_enums=formula_default_abs_enums,
            outflow_enums=outflow_by_period[period],
            previous_outflow_enums=previous_outflow,
            trace=period_trace,
            period=period,
        )
    return {period: result_by_period.get(period, {}) for period in ordered}

def _calculate_metrics_pair(
    calc_defs: list,
    extracted_current: dict[str, Optional[float]],
    extracted_prev: dict[str, Optional[float]],
    formula_default_abs_enums: set[str] | None = None,
    trace_current: dict[str, dict] | None = None,
    period_current: str = "",
    period_prev: str = "",
) -> tuple[dict[str, Optional[float]], dict[str, Optional[float]]]:
    """Compatibility helper for legacy current/previous-only callers.

    `trace_current` — происхождение ОСНОВНОГО периода (Б5): именно его число
    попадает в `value_current` строки, и подписывать строку происхождением
    сравнительного периода значило бы соврать. `period_current` — метка этого
    периода: она уезжает во входы и называет читателю колонку, которую его
    подпись действительно воспроизводит.

    `period_prev` — метка сравнительного периода. Нужна не подписи, а расчёту:
    длину периода берут из метки, и без неё соседние колонки одной строки
    считались бы по разным правилам — приведённая к году текущая против
    неприведённой предыдущей.
    """
    outflow_prev: set[str] = set()
    calc_prev = _calculate_metrics(
        calc_defs,
        extracted_prev,
        formula_default_abs_enums=formula_default_abs_enums,
        outflow_enums=outflow_prev,
        period=period_prev,
    )
    calc_current = _calculate_metrics(
        calc_defs,
        extracted_current,
        extracted_prev,
        previous_calculated=calc_prev,
        formula_default_abs_enums=formula_default_abs_enums,
        previous_outflow_enums=outflow_prev,
        trace=trace_current,
        period=period_current,
    )
    return calc_current, calc_prev
