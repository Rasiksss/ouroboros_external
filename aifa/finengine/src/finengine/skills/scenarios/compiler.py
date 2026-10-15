"""#92 (S2): компилятор инструкции сценария — «инструкция никогда не молчит».

При сохранении инструкции (PATCH / чат-тул) модель переводит текст пользователя
в диф типизированных параметров (схема #91) и возвращает честный отчёт:
  - applied:        что применил (человеческим языком);
  - not_applicable: что НЕ применимо к этой автоматизации и почему
    (кейс-триггер: «делай 20 отжиманий» обязан получить явный отказ);
  - skill_requests: что требует ДОРАБОТКИ навыка (новый источник/метрика) —
    очередь для редактора навыков (S3, #93).

Модель: z-ai/glm-5.2 (реш. Nate 24.07; сильная и дешёвая для перевода
текст→конфиг; чат/движок не трогаем). Через infproxy как все вызовы.
Best-effort: недоступность компилятора НЕ блокирует сохранение инструкции —
она продолжает работать фокусом/указаниями (проводка #90) и без него.
"""
from __future__ import annotations

import json
import logging

from finengine.config import settings

from finengine.skills.scenarios import schema as _schema
from finengine.skills.scenarios import store as _store

log = logging.getLogger(__name__)


_SYSTEM = (
    "Ты — компилятор настроек автоматизации. Пользователь пишет инструкцию "
    "свободным текстом; у автоматизации есть конечный набор типизированных "
    "параметров («ручек»). Твоя задача: перевести инструкцию в изменения ручек "
    "и честно классифицировать каждую просьбу.\n\n"
    "Верни СТРОГО JSON-объект:\n"
    '{"params": {<имя_ручки>: <значение>, ...},\n'
    ' "applied": ["что применил, по-русски, коротко", ...],\n'
    ' "not_applicable": ["что не применимо к этой автоматизации и почему", ...],\n'
    ' "skill_requests": ["что требует доработки самого навыка (новый источник, '
    'новая метрика, смена формата)", ...]}\n\n'
    "Правила:\n"
    "- В params клади ТОЛЬКО ручки из схемы и только если инструкция явно о них "
    "просит. Не трогай ручки, о которых пользователь не говорил.\n"
    "- group_flags: объект {ключ: true/false} — указывай только упомянутые ключи "
    "(false = выключить, true = включить обратно).\n"
    "- Просьбы про СОДЕРЖАТЕЛЬНЫЙ фильтр отбора (рынки, темы, компании, «только "
    "Европа») — это НЕ ручка: они работают через сам текст инструкции "
    "(фокус-фильтр). Отнеси в applied с пометкой «фокус отбора», params не меняй.\n"
    "- Всё, что не про эту автоматизацию вовсе («делай 20 отжиманий»), — в "
    "not_applicable с объяснением, что делает автоматизация.\n"
    "- Просьбы о новых возможностях (новый источник-сайт, новая колонка/метрика) "
    "— в skill_requests.\n"
    "- Пустая инструкция → все списки пустые, params пустой."
)


async def compile_instructions(scn: _store.Scenario, instructions: str) -> dict:
    """Скомпилировать инструкцию в диф параметров + отчёт. Возвращает
    {"note": str, "params_diff": dict, "applied": [...], "not_applicable": [...],
     "skill_requests": [...], "ok": bool}. Никогда не бросает."""
    kind = scn.kind
    if kind not in _schema.PARAM_SCHEMAS:
        return {"ok": False, "note": "", "params_diff": {}}
    text = (instructions or "").strip()
    if not text:
        return {"ok": True, "note": "Инструкция пуста — параметры не менялись.",
                "params_diff": {}, "applied": [], "not_applicable": [],
                "skill_requests": []}
    try:
        from finengine.llm import openrouter as llm  # noqa: PLC0415
        prompt = (
            f"## Автоматизация: {scn.name} (вид: {kind})\n\n"
            f"## Ручки (схема):\n{_schema.schema_for_prompt(kind)}\n\n"
            f"## Текущие значения params:\n"
            f"{json.dumps(scn.params_dict, ensure_ascii=False)}\n\n"
            f"## Инструкция пользователя:\n{text}"
        )
        raw, _usage = await llm.complete_with_usage(
            prompt=prompt, system=_SYSTEM, model=settings.openrouter_model,
            temperature=0.0, max_output=1200,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — компилятор best-effort
        log.warning("instruction compiler failed for %s: %s", scn.id, e)
        return {"ok": False, "params_diff": {},
                "note": ("Компилятор настроек временно недоступен — инструкция "
                         "сохранена и продолжает работать как фокус-указание.")}

    # Валидация дифа по схеме — модель не может выйти за ручки.
    diff: dict = {}
    errors: list[str] = []
    raw_params = data.get("params") or {}
    if isinstance(raw_params, dict):
        for k, v in raw_params.items():
            if k not in _schema.PARAM_SCHEMAS[kind]:
                continue
            norm, err = _schema.validate_param(kind, k, v)
            if err:
                errors.append(err)
            elif norm is not None:
                # group_flags мержим с текущими (частичный патч).
                if _schema.PARAM_SCHEMAS[kind][k]["type"] == "group_flags":
                    cur = scn.params_dict.get(k)
                    norm = {**(cur if isinstance(cur, dict) else {}), **norm}
                diff[k] = norm

    applied = [str(x) for x in (data.get("applied") or [])][:8]
    not_appl = [str(x) for x in (data.get("not_applicable") or [])][:8]
    skill_req = [str(x) for x in (data.get("skill_requests") or [])][:8]

    lines: list[str] = []
    if applied:
        lines.append("Применил: " + "; ".join(applied) + ".")
    if not_appl:
        lines.append("Не применимо: " + "; ".join(not_appl) + ".")
    if skill_req:
        lines.append("Требует доработки навыка (передам редактору): "
                     + "; ".join(skill_req) + ".")
    if errors:
        lines.append("Отклонено валидацией: " + "; ".join(errors) + ".")
    if not lines:
        lines.append("Изменений настроек не потребовалось.")
    return {"ok": True, "note": " ".join(lines), "params_diff": diff,
            "applied": applied, "not_applicable": not_appl,
            "skill_requests": skill_req}
