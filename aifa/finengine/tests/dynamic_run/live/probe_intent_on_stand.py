"""Замер разбора запросов живым классификатором на dev-стенде (часть 15, п. 6).

Вызывает настоящий `intent.parse_message` с контекстом чата (документ, роль `other`) и печатает по
строке JSON на запрос: все сырые ответы модели (в том числе «focused repair»), число вызовов, итог
разбора — виды частей и `depends_on` либо причину отказа схемы.

`VARIANT` подставляет вариант промпта в процессе, БЕЗ выкладки: `base` — как в коде; `v1` — исключение
в основном промпте (не помогло); `v2`/`v3` — абзац про `custom_analysis` в промпте ремонта
`_COMPOUND_CLAUSE_REPAIR_SYSTEM` (v3 — рабочий). Запуск — только против dev-контейнера (в имени цели
нет `prod`), запросы — base64 от JSON-массива:

    ssh <dev> "sudo docker exec -i -e VARIANT=v3 -e PROBE_Q=<base64> finengine-backend-1 python -" < этот_файл
"""
import asyncio
import base64
import json
import os

from finengine.skills.atoms import intent

DOC_NAME = "ПрайсвотерхаусКуперс Аудит"

V1_ANCHOR = '"посчитай/рассчитай" — "calculate_ad_hoc_metric".'
V1_TEXT = (
    " Исключение: статистика и любая обработка ряда значений (коэффициент вариации, медиана, среднее, "
    "стандартное отклонение, корреляция, регрессия, темпы роста по периодам), а также всё, что требует "
    'внешних данных, — НЕ "calculate_ad_hoc_metric": формулу из + - * / они не выражают. Такая просьба — '
    '"custom_analysis" (в составном запросе — отдельная цель с depends_on).'
)

V2_ANCHOR = "Узкоспециализированный код допустим только"
V2_TEXT = (
    "Если черновик имеет intent_kind=custom_analysis, СОХРАНИ его: статистика и обработка ряда значений "
    "(коэффициент вариации, медиана, среднее, стандартное отклонение, корреляция, регрессия, темпы роста), "
    "а также всё, что требует внешних данных, — это custom_analysis, а не calculate_ad_hoc_metric "
    "(формула из + - * / их не выражает). Для custom_analysis targets=[], ad_hoc_formula=null, а текст "
    "анализа дословно положи в unresolved; depends_on — goal_ref цели, поверх результата которой анализ "
    "считается.\n\n"
)

V3_TEXT = (
    "Если черновик имеет intent_kind=custom_analysis, СОХРАНИ его: статистика и обработка ряда значений "
    "(коэффициент вариации, медиана, среднее, стандартное отклонение, корреляция, регрессия), а также всё, "
    "что требует внешних данных, — это custom_analysis, а не calculate_ad_hoc_metric (формула из + - * / "
    "их не выражает). Для custom_analysis targets=[], ad_hoc_formula=null, а текст анализа дословно "
    'положи в unresolved как МАССИВ из одной строки: ["<текст анализа>"]; depends_on — массив с goal_ref '
    "цели, поверх результата которой анализ считается.\n\n"
)

VARIANT = os.environ.get("VARIANT", "base")
if VARIANT == "v1":
    assert V1_ANCHOR in intent._SYSTEM_PROMPT
    intent._SYSTEM_PROMPT = intent._SYSTEM_PROMPT.replace(V1_ANCHOR, V1_ANCHOR + V1_TEXT, 1)
if VARIANT == "v2":
    assert V2_ANCHOR in intent._COMPOUND_CLAUSE_REPAIR_SYSTEM
    intent._COMPOUND_CLAUSE_REPAIR_SYSTEM = intent._COMPOUND_CLAUSE_REPAIR_SYSTEM.replace(
        V2_ANCHOR, V2_TEXT + V2_ANCHOR, 1
    )


if VARIANT == "v3":
    assert V2_ANCHOR in intent._COMPOUND_CLAUSE_REPAIR_SYSTEM
    intent._COMPOUND_CLAUSE_REPAIR_SYSTEM = intent._COMPOUND_CLAUSE_REPAIR_SYSTEM.replace(
        V2_ANCHOR, V3_TEXT + V2_ANCHOR, 1
    )


async def one(query):
    raws = []
    original = intent._default_complete

    async def spy(system, prompt, **kwargs):
        raw = await original(system, prompt, **kwargs)
        raws.append(raw)
        return raw

    context = intent.IngressContext(documents=((DOC_NAME, True),), ui_role="other", history=())
    try:
        decision = await intent.parse_message(query, context=context, complete_fn=spy)
        goal = decision.goal
        result = {"ok": True, "route": decision.route,
                  "goal": goal.model_dump(mode="json") if goal is not None else None}
    except Exception as exc:  # noqa: BLE001 — отказ разбора — тоже результат замера
        result = {"ok": False, "err": f"{type(exc).__name__}: {str(exc)[:400]}"}
    print(json.dumps({"q": query, "n_calls": len(raws), "raws": raws, "result": result}, ensure_ascii=False),
          flush=True)


async def main():
    for query in json.loads(base64.b64decode(os.environ["PROBE_Q"]).decode()):
        await one(query)


asyncio.run(main())
