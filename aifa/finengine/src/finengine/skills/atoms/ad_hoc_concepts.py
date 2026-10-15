"""Ad-hoc понятия и формулы, придуманные моделью (Срез 31, П4/П5).

**Что делает модель.** Когда пользователь просит извлечь или посчитать то, чего
нет в реестре, модель из своих знаний придумывает имя, синонимы и определение
понятия (П5), а для расчёта — формулу и её состав (П4): пользователь формулу
называть НЕ обязан.

**Что делает код.** Этот модуль — детерминированная граница доверия. Он не даёт
модели выдумать факт реестра или небезопасное выражение:

* код реестра, названный моделью, проверяется по реальным реестрам всех профилей;
* если кода нет, компонент сопоставляется с реестром по ТОЧНОМУ имени/синониму
  (без нечёткого поиска) — иначе остаётся ad-hoc понятием;
* выражение — только числа, `+ - * / ( )` и ASCII-символы; каждый символ обязан
  быть описан компонентом и наоборот;
* итоговая строка для двухстадийного экстрактора (`metric_row`) имеет ту же форму,
  что строка реестра, — `locate`/`read.value` не знают, что понятие ad-hoc.

Числа здесь не считаются и не извлекаются — только описание того, ЧТО искать.
"""
from __future__ import annotations

import ast
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from functools import lru_cache

from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec

EXTRA_KEY = "ad_hoc_concepts"

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SYMBOL_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_SAFE_EXPRESSION_RE = re.compile(r"^[A-Z0-9_+\-*/().\s]+$")
_VALUE_KINDS = ("flow", "stock", "ratio", "other")
_PERIOD_TYPE_BY_KIND = {"flow": "duration", "stock": "point"}
_MAX_ALIASES = 12


def _norm_label(text: object) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(t for t in re.split(r"[^\w]+", s, flags=re.UNICODE) if t)


def _clean_list(values: object, *, limit: int = _MAX_ALIASES) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values if isinstance(values, list) else []:
        text = " ".join(str(raw or "").split())
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Реестр как точный индекс
@lru_cache(maxsize=1)
def _registry_indexes() -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile  # noqa: PLC0415

    enums: set[str] = set()
    by_label: dict[str, set[str]] = {}
    for profile_id in PROFILE_IDS:
        try:
            registry = registry_for_profile(profile_id)
        except (FileNotFoundError, ValueError):
            continue
        for row in registry.get("extractable", []):
            enum = str(row.get("enum") or "").strip().upper()
            if not enum:
                continue
            enums.add(enum)
            labels = [row.get("name_ru"), row.get("name_en"),
                      *(row.get("aliases_ru") or []), *(row.get("aliases_en") or [])]
            for label in labels:
                key = _norm_label(label)
                if key:
                    by_label.setdefault(key, set()).add(enum)
        for entry in registry.get("calculated", []):
            if not isinstance(entry, (list, tuple)) or not entry:
                continue
            enum = str(entry[0]).strip().upper()
            if not enum:
                continue
            enums.add(enum)
            key = _norm_label(entry[1] if len(entry) > 1 else "")
            if key:
                by_label.setdefault(key, set()).add(enum)
    return frozenset(enums), {k: frozenset(v) for k, v in by_label.items()}


def all_registry_enums() -> frozenset[str]:
    """Коды показателей объединения реестров ВСЕХ профилей (проверка «код существует»)."""
    return _registry_indexes()[0]


def registry_code_for(labels: Iterable[str]) -> str | None:
    """Единственный код реестра, чьё имя/синоним ТОЧНО совпадает с одним из `labels`.

    Нечёткий поиск здесь намеренно не используется: неверное сопоставление
    молча подменило бы понятие пользователя чужим показателем. Неоднозначное
    совпадение (несколько кодов) — тоже `None`: понятие остаётся ad-hoc.
    """
    _, by_label = _registry_indexes()
    found: set[str] = set()
    for label in labels:
        key = _norm_label(label)
        if key:
            found.update(by_label.get(key, ()))
    return next(iter(found)) if len(found) == 1 else None


# ---------------------------------------------------------------------------
# Понятие (П5)
def concept_spec_from_payload(
    payload: object, *, fallback_term: str, key: str = "",
) -> AdHocConceptSpec | None:
    """`AdHocConceptSpec` из JSON-объекта ответа модели (или `None`, если это не объект)."""
    if not isinstance(payload, Mapping):
        return None
    name_ru = " ".join(str(payload.get("name_ru") or "").split())
    name_en = " ".join(str(payload.get("name_en") or "").split())
    term = name_ru or name_en or " ".join(str(fallback_term or "").split())
    if not term:
        return None
    value_kind = str(payload.get("value_kind") or "other").strip().lower()
    return AdHocConceptSpec(
        term=term, key=key, name_ru=name_ru, name_en=name_en,
        aliases_ru=_clean_list(payload.get("aliases_ru")),
        aliases_en=_clean_list(payload.get("aliases_en")),
        definition=" ".join(str(payload.get("definition") or "").split()),
        hint=" ".join(str(payload.get("hint") or "").split()),
        value_kind=value_kind if value_kind in _VALUE_KINDS else "other",  # type: ignore[arg-type]
    )


def metric_row(spec: AdHocConceptSpec) -> dict:
    """Строка метрики в форме реестра для `locate`/`read.value` (двухстадийный экстрактор)."""
    row = {
        "enum": spec.concept_key,
        # User-defined labels have no registry-curated disambiguation.  The
        # second stage uses this internal marker to isolate them physically;
        # otherwise similarly named rows can borrow a neighbour's value.
        "ad_hoc": True,
        "name_ru": spec.name_ru or spec.term,
        "name_en": spec.name_en or "",
        "aliases_ru": list(spec.aliases_ru),
        "aliases_en": list(spec.aliases_en),
        "description": spec.definition,
        "hint": spec.hint,
        "period_type": _PERIOD_TYPE_BY_KIND.get(spec.value_kind, ""),
    }
    return row


def needs_description(spec: AdHocConceptSpec) -> bool:
    """Понятие ещё нужно описать моделью (синонимы, определение): его нет в реестре и
    описания нет вовсе. Понятия реестра ищутся по выверенным записям реестра."""
    return spec.concept_key.strip().upper() not in all_registry_enums() and not (
        spec.aliases_ru or spec.aliases_en or spec.definition
    )


def dump_specs(specs: Iterable[AdHocConceptSpec]) -> str:
    """Сериализация для `NodeArgs.extra` (значения `extra` — только строки)."""
    return json.dumps(
        [spec.model_dump(mode="json") for spec in specs], ensure_ascii=False, sort_keys=True,
    )


def load_specs(extra: Mapping[str, str]) -> dict[str, AdHocConceptSpec]:
    """`{КЛЮЧ_В_ВЕРХНЕМ_РЕГИСТРЕ: spec}` из `NodeArgs.extra`; мусор игнорируется."""
    raw = (extra or {}).get(EXTRA_KEY)
    if not raw:
        return {}
    try:
        items = json.loads(raw)
    except ValueError:
        return {}
    out: dict[str, AdHocConceptSpec] = {}
    for item in items if isinstance(items, list) else []:
        try:
            spec = AdHocConceptSpec.model_validate(item)
        except ValueError:
            continue
        out[spec.concept_key.strip().upper()] = spec
    return out


# ---------------------------------------------------------------------------
# Формула (П4)
class FormulaRejected(ValueError):
    """Формула модели недоверенна; текст — конкретная причина (её получает модель)."""


def _build_formula(payload: object, fallback_term: str) -> AdHocFormulaSpec:
    if not isinstance(payload, Mapping):
        raise FormulaRejected("ad_hoc_formula — не JSON-объект")
    expression = " ".join(str(payload.get("expression") or "").split()).upper()
    if not _expression_is_safe(expression):
        raise FormulaRejected(
            "expression пуст или содержит недопустимые конструкции: разрешены только числа, круглые "
            "скобки, + - * / и символы из ЗАГЛАВНЫХ латинских букв, цифр и _ (без функций, степеней и условий)"
        )
    symbols = set(_SYMBOL_RE.findall(expression))
    universe = all_registry_enums()
    raw_components = payload.get("components")
    if not symbols:
        term = " ".join(str(payload.get("name_ru") or "").split()) or " ".join(str(fallback_term or "").split())
        if not term:
            raise FormulaRejected("не задано name_ru формулы")
        return AdHocFormulaSpec(
            term=term, expression=expression, dependencies=[],
            assumptions=" ".join(str(payload.get("assumptions") or "").split()),
            name_ru=" ".join(str(payload.get("name_ru") or "").split()),
            name_en=" ".join(str(payload.get("name_en") or "").split()),
            definition=" ".join(str(payload.get("definition") or "").split()),
            unit=" ".join(str(payload.get("unit") or "").split()), components=[],
        )
    if not raw_components:
        # Краткая форма: формула только над показателями реестра — достаточно
        # списка их кодов. Неизвестный код без описания доверия не заслуживает.
        deps = payload.get("dependencies")
        if isinstance(deps, list) and deps and all(str(d).strip().upper() in universe for d in deps):
            raw_components = [{"key": str(d).strip(), "registry_code": str(d).strip()} for d in deps]
    if isinstance(raw_components, Mapping):
        # Плоская форма «символ → название» (или «символ → объект»): устойчивее
        # вложенного массива — реальная модель разваливала массив объектов
        # (живая проверка M2, 24.09.2026). Приводится к форме списка объектов.
        flat: list[object] = []
        for symbol, value in raw_components.items():
            if isinstance(value, str) and value.strip():
                flat.append({"key": symbol, "name_ru": value})
            elif isinstance(value, Mapping):
                flat.append({**value, "key": symbol})
            else:
                flat.append(None)
        raw_components = flat
    if not isinstance(raw_components, list) or not raw_components:
        raise FormulaRejected(
            "components пуст: нужен объект «символ → название показателя» на КАЖДЫЙ символ выражения, "
            'например {"DEBT": "Заёмные средства", "ASSETS": "Итого активов"}'
        )

    components: list[AdHocConceptSpec] = []
    rename: dict[str, str] = {}
    for index, raw in enumerate(raw_components, start=1):
        if not isinstance(raw, Mapping):
            got = "null" if raw is None else type(raw).__name__
            raise FormulaRejected(
                f"компонент {index} — не объект (получено {got}): заполни key, name_ru, aliases, definition"
            )
        key = str(raw.get("key") or "").strip().upper()
        if not _KEY_RE.match(key):
            raise FormulaRejected(
                f"key компонента {index} ({raw.get('key')!r}) должен состоять из ЗАГЛАВНЫХ латинских "
                "букв, цифр и _ и начинаться с буквы"
            )
        spec = concept_spec_from_payload(raw, fallback_term=key)
        if spec is None:  # pragma: no cover — raw уже Mapping, а fallback_term=key непуст
            raise FormulaRejected(f"компонент {index} не описан")
        code = str(raw.get("registry_code") or "").strip().upper()
        if code not in universe:
            code = registry_code_for([spec.name_ru, spec.name_en, *spec.aliases_ru, *spec.aliases_en]) or ""
        final_key = code or key
        rename[key] = final_key
        components.append(spec.model_copy(update={"key": final_key}))

    if set(rename) != symbols:
        raise FormulaRejected(
            f"символы выражения {sorted(symbols)} не совпадают с key компонентов {sorted(rename)}: "
            "каждый символ обязан иметь компонент и наоборот"
        )
    expression = _SYMBOL_RE.sub(lambda m: rename.get(m.group(0), m.group(0)), expression)

    unique: dict[str, AdHocConceptSpec] = {}
    for spec in components:
        unique.setdefault(spec.concept_key, spec)
    term = " ".join(str(payload.get("name_ru") or "").split()) or " ".join(str(fallback_term or "").split())
    if not term:
        raise FormulaRejected("не задано name_ru формулы")
    return AdHocFormulaSpec(
        term=term, expression=expression, dependencies=list(unique),
        assumptions=" ".join(str(payload.get("assumptions") or "").split()),
        name_ru=" ".join(str(payload.get("name_ru") or "").split()),
        name_en=" ".join(str(payload.get("name_en") or "").split()),
        definition=" ".join(str(payload.get("definition") or "").split()),
        unit=" ".join(str(payload.get("unit") or "").split()),
        components=list(unique.values()),
    )


def formula_spec_from_payload(payload: object, *, fallback_term: str) -> AdHocFormulaSpec | None:
    """`AdHocFormulaSpec` из ответа модели, либо `None`, если формула недоверенна.

    Недоверенная формула отвергается ЦЕЛИКОМ: выражение, ссылающееся на
    отброшенный символ, осталось бы со свободной переменной — считать нечего.
    """
    try:
        return _build_formula(payload, fallback_term)
    except FormulaRejected:
        return None


def formula_rejection_reason(payload: object) -> str:
    """Конкретная причина отказа для модели (`""` — формула принята). Возвращается
    модели в единственной повторной попытке разбора."""
    try:
        _build_formula(payload, "формула")
    except FormulaRejected as exc:
        return str(exc)
    return ""


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)


def _expression_is_safe(expression: str) -> bool:
    """Белый список по AST — то же множество конструкций, что принимает
    `formula_engine._parse_formula_expr`: числа, имена, `+ - * /`, унарный знак,
    скобки. Всё остальное (вызовы, атрибуты, `**`, `//`, условия) отвергается."""
    if not expression or not _SAFE_EXPRESSION_RE.match(expression):
        return False
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load)):
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARY):
            continue
        if isinstance(node, (*_ALLOWED_BINOPS, *_ALLOWED_UNARY)):
            continue
        if isinstance(node, ast.Name):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            continue
        return False
    return True


__all__ = [
    "EXTRA_KEY",
    "all_registry_enums",
    "concept_spec_from_payload",
    "dump_specs",
    "formula_rejection_reason",
    "formula_spec_from_payload",
    "load_specs",
    "metric_row",
    "needs_description",
    "registry_code_for",
]
