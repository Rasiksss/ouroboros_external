"""#91 (S1): схема параметров скрипт-бэкед сценариев — единый источник правды.

Потребители схемы:
  - валидация params в PATCH /scenarios/{id} и в туле fe_scenario_settings
    (одни правила, один код);
  - компилятор инструкции (#92): модель видит, какие «ручки» существуют,
    и переводит текст пользователя в диф параметров;
  - фронт-редакторы (DailySettingsEditor / CalendarWindowEditor) — рендерят
    ровно эти ручки; при добавлении ручки сюда — добавить поле и там.

Формат ручки:
  {"type": "int",  "min": .., "max": .., "label": .., "desc": ..}
  {"type": "enum", "values": [..], ...}
  {"type": "group_flags", "keys": {key: label}, ...}  # объект {key: bool}
"""
from __future__ import annotations

from typing import Any

PARAM_SCHEMAS: dict[str, dict[str, dict]] = {
    "ir_daily": {
        "since_hours": {
            "type": "int", "min": 6, "max": 336, "default": 48,
            "label": "Период сбора, часов назад",
            "desc": "За сколько часов назад собирать новости из источников.",
        },
        "source_groups": {
            "type": "group_flags",
            "keys": {"official": "Официальные", "media": "СМИ",
                     "telegram": "Telegram", "aggregator": "Агрегаторы"},
            "label": "Группы источников",
            "desc": "Какие группы источников опрашивать (false = группа выключена).",
        },
        "macro_mode": {
            "type": "enum", "values": ["both", "week_only", "full"], "default": "both",
            "label": "Макропоказатели",
            "desc": "both — обновления недели + полный дашборд; week_only — только "
                    "обновившиеся на этой неделе; full — только полный дашборд.",
        },
    },
    "ir_calendar": {
        "back_days": {
            "type": "int", "min": 1, "max": 120, "default": 14,
            "label": "Окно назад, дней",
            "desc": "Сколько дней назад искать уже прошедшие события.",
        },
        "fwd_days": {
            "type": "int", "min": 1, "max": 120, "default": 14,
            "label": "Окно вперёд, дней",
            "desc": "На сколько дней вперёд искать анонсированные события.",
        },
        "event_groups": {
            "type": "group_flags",
            "keys": {"reporting": "Отчётность", "investor_day": "День инвестора",
                     "other": "Прочее"},
            "label": "Группы событий",
            "desc": "Какие группы событий показывать в календаре.",
        },
    },
}


def validate_param(kind: str, key: str, value: Any) -> tuple[Any, str | None]:
    """Один параметр → (нормализованное значение, None) или (None, ошибка).
    value=None легально везде — означает «снять настройку» (вернуть дефолт)."""
    spec = (PARAM_SCHEMAS.get(kind) or {}).get(key)
    if spec is None:
        # Неизвестные ключи не отбрасываем жёстко (служебные вроде _compiled_note),
        # но и не нормализуем.
        return value, None
    if value is None:
        return None, None
    t = spec["type"]
    if t == "int":
        try:
            return max(spec["min"], min(spec["max"], int(value))), None
        except (TypeError, ValueError):
            return None, f"{key}: нужно число {spec['min']}..{spec['max']}"
    if t == "enum":
        if value not in spec["values"]:
            return None, f"{key}: одно из {spec['values']}"
        return value, None
    if t == "group_flags":
        if not isinstance(value, dict):
            return None, f"{key}: нужен объект {{ключ: true/false}}"
        known = set(spec["keys"])
        out = {k: bool(v) for k, v in value.items() if k in known}
        return out, None
    return value, None


def schema_for_prompt(kind: str) -> str:
    """Компактное описание ручек для промпта компилятора (#92)."""
    import json
    schema = PARAM_SCHEMAS.get(kind) or {}
    lines = []
    for key, spec in schema.items():
        lines.append({
            "param": key, "type": spec["type"], "label": spec["label"],
            "desc": spec["desc"],
            **({"min": spec["min"], "max": spec["max"]} if spec["type"] == "int" else {}),
            **({"values": spec["values"]} if spec["type"] == "enum" else {}),
            **({"keys": spec["keys"]} if spec["type"] == "group_flags" else {}),
        })
    return json.dumps(lines, ensure_ascii=False, indent=1)
