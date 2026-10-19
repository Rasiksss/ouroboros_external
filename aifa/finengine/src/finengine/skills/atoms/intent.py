"""`intent.parse` — Срез 4 (B4/A6, docs/build-plan/14-srez-04-graf-istochnik-
istiny.md). Переводит свободный запрос пользователя в `GoalIR`: не строит
исполняемый план и не выполняет финансовую работу (это делают `root_from_goal`
+ `GraphPlanner`/`GraphExecutor`, Срез 3 + этот файл ниже).

Инженерный принцип карточки: «Свободный язык поглощается один раз на входе;
дальше система работает со структурой». Модель здесь НЕ выбирает финансово
что-либо — она сопоставляет слова пользователя с ЗАКРЫТЫМ словарём, который ей
дают (`candidate_concepts`), а детерминированный код после ответа проверяет,
что она действительно выбрала из этого словаря, а не придумала код.

Срез 31 (Ingress v2, docs/build-plan/31-vozvrat-k-zamyslu-vladeltsa-ingress-v2.md):
атом стал ПЕРВЫМ шагом для КАЖДОГО сообщения пользователя и одновременно
маршрутизатором — модель решает, передать запрос планировщику (`route="plan"`)
или ответить самой по контексту (`route="answer"`). Если запрос подходит под один
из GoalType, модель обязана выбрать `plan`. Период по умолчанию не спрашивается:
пустой `period_ref` означает «все доступные периоды». Для показателей и формул
вне реестра описание (имя, синонимы, определение, формулу и её состав) придумывает
модель; детерминированный код проверяет его в `ad_hoc_concepts.py`.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, NamedTuple

from finengine.config import settings
from finengine.contracts import (
    AdHocConceptSpec, AdHocFormulaSpec, AmbiguityNote, CorrectionIntent, GoalClause, GoalIR, TargetNode,
)
from finengine.roles.access import EXTRACTION_ROLES, ROLE_LABELS
from finengine.skills.atoms.ad_hoc_concepts import (
    EXTRA_KEY as AD_HOC_CONCEPTS_KEY,
    concept_spec_from_payload,
    dump_specs,
    formula_rejection_reason,
    formula_spec_from_payload,
    needs_description,
)
from finengine.skills.metric_schema import PROFILE_IDS
from finengine.skills.registry_find import find_metrics
from finengine.skills.spec.target import Target

ATOM_VERSION = "1.0.0"

# Закрытые перечисления, которые модель видит буквально в системном промпте —
# то же самое множество, что `contracts.intent.IntentKind`/`roles.access.
# EXTRACTION_ROLES`, не отдельно придуманный список. Retrofit v9: состав —
# каталог 13 GoalType (docs/build-plan/26-arhitekturnaya-fiksaciya-v9.md, §1
# — двенадцать значений более ранней редакции плюс `monitor_changes`) плюс
# `form` сверх каталога (см. комментарий у `IntentKind` в `contracts/intent.py`
# — почему `form` сохранён отдельно, а не слит с `full_analysis`).
# `custom_analysis` (02.10.2026) — 14-е значение: обработка сверх каталога атомов, без
# детерминированного корня (см. `_NOT_YET_WIRED_KINDS`), цель уходит в DynamicRun.
_INTENT_KINDS: tuple[str, ...] = (
    "full_analysis",
    "extract_registered_metric",
    "extract_ad_hoc_metric",
    "calculate_registered_metric",
    "calculate_ad_hoc_metric",
    "extract_structure",
    "document_qa",
    "compare_metrics",
    "explain_change",
    "validate_report",
    "plan_fact_analysis",
    "monitor_changes",
    "form",
    "custom_analysis",
)
# Из этих значений `targets[].concept` заполняется по кандидатам (та же,
# НЕИЗМЕНЁННАЯ проверка `_finalize_goal`, что раньше обслуживала одно
# значение `point_metric`) — остальные всегда оставляют `targets` пустым.
_METRIC_KINDS_WITH_TARGETS: tuple[str, ...] = ("extract_registered_metric", "calculate_registered_metric")
# Retrofit v9 (R4): ad-hoc виды больше НЕ строят корень из (всегда пустого)
# `goal.targets` наравне с registered — они создают временный `AdHocConceptSpec`/
# `AdHocFormulaSpec` из формулировки пользователя (`root_from_goal`, ниже).
_METRIC_KINDS_AD_HOC: tuple[str, ...] = ("extract_ad_hoc_metric", "calculate_ad_hoc_metric")
_CALCULATE_KINDS: tuple[str, ...] = ("calculate_registered_metric", "calculate_ad_hoc_metric")
# Распознаются intent.parse (каталог 13 GoalType, `26-arhitekturnaya-
# fiksaciya-v9.md`), но producer для корня графа для них ещё не реализован —
# `root_from_goal` поднимает явную ошибку вместо того, чтобы выдумывать
# несуществующий графовый концепт. `monitor_changes` здесь намеренно отсутствует:
# Срез 32 подключает его к обычному графу через `PeriodChangeReport` (изменения
# между периодами одного отчёта).
_NOT_YET_WIRED_KINDS: tuple[str, ...] = (
    "extract_structure", "document_qa", "compare_metrics",
    "validate_report", "plan_fact_analysis", "custom_analysis",
)
_ROLES: tuple[str, ...] = tuple(EXTRACTION_ROLES)
_MAX_RETRIES = 1  # «после ОДНОЙ разрешённой повторной попытки» — карточка, дословно.


class IntentParseError(RuntimeError):
    """Модель не выдала валидную схему `GoalIR` даже после одной разрешённой
    повторной попытки (критерий готовности: «невалидный ответ модели
    переводится в явную ошибку, а не чинится скрытой эвристикой»)."""


# ---------------------------------------------------------------------------
# Ограниченный словарь кандидатов — то, что модель получает ВМЕСТО всего
# реестра (карточка).
def _norm_tokens(text: str) -> list[str]:
    s = unicodedata.normalize("NFKC", text or "").casefold()
    return [t for t in re.split(r"[^\w]+", s, flags=re.UNICODE) if t]


def candidate_concepts(raw_query: str, *, max_ngram: int = 4, limit: int = 60) -> dict[str, dict]:
    """`enum -> строка реестра` — объединение результатов `find_metrics` по
    ВСЕМ `PROFILE_IDS` для всех n-грамм запроса длиной 1..`max_ngram`.

    Профиль ячейки на этом шаге ещё не известен — классификация происходит
    ПОЗЖЕ, как часть графа (двухпроходное раскрытие, A1.5), а не раньше
    `intent.parse`. `find_metrics` сам по себе — поиск по реестру ОДНОЙ ячейки
    (его собственный докстринг), поэтому кандидаты собираются объединением по
    всем ячейкам, не переизобретая его скоринг: этот атом вызывает его как
    есть, по очереди, и просто мёржит результат по `enum`.

    N-граммы — дешёвый суррогат сегментации запроса на «сколько метрик
    названо» без отдельного NLU-шага: для типичного запроса в 5-15 токенов
    это десятки коротких фраз, каждая — локальный поиск по словарю (без сети,
    без модели), и итоговый словарь кандидатов на практике остаётся малым —
    именно потому, что `find_metrics` возвращает только то, что реально
    совпало по словам, а не всё подряд.
    """
    tokens = _norm_tokens(raw_query)
    phrases: list[str] = []
    seen: set[str] = set()
    # Longer phrases go first. A compound request can yield hundreds of
    # n-grams while the model-facing catalogue is deliberately capped. With
    # one-word phrases first, broad cross-profile matches filled the cap before
    # exact names such as «коммерческие и управленческие расходы» were seen.
    for n in range(max_ngram, 0, -1):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i : i + n])
            # Короче 4 знаков — тот же порог, что `find_metrics._same_word`
            # уже применяет к отдельным словам: такая фраза несёт слишком
            # мало сигнала и на самом слабом уровне скоринга (`_SUBSTR_DESC`
            # — подстрока где угодно в description) находит случайные
            # совпадения ("бла" внутри "нал-ОГООБЛА-гаемых"), а не показатель.
            if len(phrase) >= 4 and phrase not in seen:
                seen.add(phrase)
                phrases.append(phrase)
    if not phrases:
        return {}

    best: dict[str, tuple[float, int, dict]] = {}
    encounter_order = 0
    for profile_id in PROFILE_IDS:
        try:
            hits = find_metrics(phrases, profile_id, limit=3)
        except (FileNotFoundError, ValueError):
            continue
        for rows in (hits.get("matches") or {}).values():
            for row in rows:
                enum = str(row.get("enum") or "")
                score = float(row.get("match_score") or 0.0)
                previous = best.get(enum)
                if enum and (previous is None or score > previous[0]):
                    best[enum] = (score, encounter_order, row)
                encounter_order += 1
    ranked = sorted(best.items(), key=lambda item: (-item[1][0], item[1][1], item[0]))
    return {enum: row for enum, (_score, _order, row) in ranked[:limit]}


# ---------------------------------------------------------------------------
# Срез 31: контекст диалога вместо «контекста уточнения периода». Раньше бэкенд
# хранил в `Session` снимки прошлого разбора (`clarification`/
# `last_resolved_goal`) и подмешивал их в повторный разбор. Теперь модель видит
# последние реплики диалога, список документов и выбранную в UI роль — короткая
# реплика («а за прошлый год?», «и по прибыли») понятна из истории, а не из
# специальных снимков состояния.
_MAX_HISTORY_TURNS = 6
_MAX_TURN_CHARS = 400


@dataclass(frozen=True)
class IngressContext:
    """Всё, что модель-маршрутизатор получает сверх самого сообщения.

    `documents` — пары `(отображаемое имя, активный ли)`; `ui_role` — роль,
    выбранная пользователем в интерфейсе (`""` — не выбрана); `history` — последние
    реплики от старых к новым: `("user"|"assistant", текст)`."""

    documents: tuple[tuple[str, bool], ...] = ()
    ui_role: str = ""
    history: tuple[tuple[str, str], ...] = ()


class IntentDecision(NamedTuple):
    """Решение маршрутизатора: `plan` — `goal` передаётся планировщику; `answer` —
    модель решила ответить сама по контексту (`goal is None`)."""

    route: Literal["plan", "answer"]
    goal: GoalIR | None
    reason: str = ""


def _ui_role_code(ui_role: str) -> str:
    """Роль UI как точный код (`other|risks|baifp|die|cib|dgr`) или `""`.

    Только точные коды: `normalize_extraction_role` молча превращает любую
    неизвестную строку в `other`, а выбранная пользователем роль не должна
    подменяться догадкой. «Все показатели» (`all`) — не роль-сужение: `""`."""
    code = (ui_role or "").strip().lower()
    return code if code in _ROLES else ""


def _context_prompt_block(context: IngressContext) -> str:
    lines = ["## Контекст диалога"]
    if context.documents:
        lines.append("Прикреплённые документы:")
        lines.extend(f"- «{name}»{' (активный — с ним и работай, если запрос не называет другой)' if active else ''}"
                     for name, active in context.documents)
    else:
        lines.append("Документы: не прикреплены.")
    role = _ui_role_code(context.ui_role)
    if role:
        lines.append(
            f"Роль пользователя, выбранная в интерфейсе: {ROLE_LABELS.get(role, role)} ({role}). "
            "Это источник истины для состава полного анализа и формы; не заменяй её "
            "ролью, упомянутой или угаданной из текста. Верни этот же код role."
        )
    turns = context.history[-_MAX_HISTORY_TURNS:]
    if turns:
        lines.append("Последние реплики (от старых к новым):")
        for who, text in turns:
            clipped = " ".join(str(text or "").split())[:_MAX_TURN_CHARS]
            lines.append(f"{'Пользователь' if who == 'user' else 'Ассистент'}: {clipped}")
    return "\n".join(lines)


def _previous_user_turns(context: IngressContext | None, *, limit: int = 2) -> list[str]:
    if context is None:
        return []
    texts = [text for who, text in context.history if who == "user" and str(text or "").strip()]
    return texts[-limit:]


def _context_candidates(raw_query: str, context: IngressContext | None) -> dict[str, dict]:
    """Кандидаты по сообщению И по последним репликам пользователя: короткое
    «а за 2025?» само показателя не называет — его держит предыдущая реплика.
    Сообщение — первым (его совпадения важнее), общий предел прежний."""
    merged = dict(candidate_concepts(raw_query))
    for text in reversed(_previous_user_turns(context)):
        for enum, row in candidate_concepts(text).items():
            merged.setdefault(enum, row)
    return dict(list(merged.items())[:60])


def _candidate_prompt_block(candidates: dict[str, dict]) -> str:
    if not candidates:
        return "(в этом запросе не нашлось ни одного совпадения со словарём показателей)"
    lines = []
    for enum, row in candidates.items():
        names = row.get("name_ru") or row.get("name_en") or enum
        lines.append(f"- {enum}: {names}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Модельный вызов — единый шлюз (`finengine.llm.openrouter`), тот же, каким
# уже пользуется `two_stage_extractor` (методология C4: «Все вызовы модели
# идут через единый шлюз»).
_SYSTEM_PROMPT = """Ты — модуль разбора запроса финансового аналитика (intent.parse) и маршрутизатор.
КАЖДОЕ сообщение пользователя сначала попадает к тебе. Ты решаешь одно из двух: передать запрос
планировщику (route="plan") или ответить самому по контексту диалога (route="answer").
Ты НЕ анализируешь отчётность, НЕ извлекаешь числа, НЕ строишь план шагов и НЕ пишешь ответ
пользователю — ты только определяешь маршрут и структуру намерения.

СОСТАВНОЙ ЗАПРОС. Если пользователь просит результаты РАЗНЫХ intent_kind, основной результат
верни в полях верхнего уровня, а каждый следующий — отдельным объектом additional_goals. Дай им
локальные goal_ref (`goal-1`, `goal-2`, ...). В depends_on перечисляй goal_ref только когда второй
результат действительно использует результат первого; независимые просьбы имеют depends_on=[].
Не называй атомы и шаги. Например, «извлеки прибыль и посчитай 2+2» — независимые цели, а
«извлеки прибыль и рассчитай рентабельность по этой прибыли» — расчёт зависит от извлечения.
Анализ поверх извлечённого показателя — тоже составной запрос: «извлеки выручку и посчитай для неё
коэффициент вариации» — goal-1 extract_registered_metric (выручка) и goal-2 custom_analysis с
depends_on=["goal-1"], текст анализа дословно в её unresolved. Допустима и одна цель custom_analysis
на всю просьбу (см. её описание ниже). Анализ не теряй и не заменяй извлечением.
Каждый объект additional_goals заполняй ПОЛНОСТЬЮ по той же схеме, что основную цель. Все поля-
массивы (`targets`, `analysis_concepts`, `unresolved`, `ambiguities`, `depends_on`) ВСЕГДА должны
быть JSON-массивами, хотя бы пустыми `[]`, и НИКОГДА `null`. Для `calculate_ad_hoc_metric`
`ad_hoc_formula` должна быть заполнена; `null` допустим только если расчёт невозможно выразить
проверяемой арифметикой и конкретная причина записана в `unresolved`.

МАРШРУТ (route):
- "plan" — запрос пользователя подходит под ОДИН ИЗ intent_kind из списка ниже: извлечь или посчитать
  показатель (в том числе которого нет в реестре), полный анализ, форма, структура/таблица раскрытия,
  вопрос по содержимому документа, сравнение, объяснение изменения, проверка отчёта, план-факт,
  наблюдение за изменениями. Такой запрос ты ОБЯЗАН вернуть с route="plan" и заполненным intent_kind:
  он передаётся планировщику, отвечать на него самому нельзя.
- "answer" — запрос НЕ подходит ни под один intent_kind: приветствие, вопрос о твоих возможностях,
  просьба пояснить или переформулировать уже данный ответ, общий вопрос, для которого не нужны данные
  из отчёта, поиск новостей/календаря/отчётности в интернете, настройки. Тогда intent_kind=null,
  targets=[], остальные поля пустые.
- Сомневаешься между "plan" и "answer", а запрос касается показателей или данных из документа, —
  выбирай "plan".
Учитывай блок «Контекст диалога» (документы, роль, последние реплики): короткая реплика вроде
«а за прошлый год?» или «и по прибыли тоже» — продолжение предыдущего запроса; собери цель целиком.

ПЕРИОД. period_ref заполняй ТОЛЬКО периодом, который пользователь назвал в своей реплике (или в
предыдущей, если текущая — её продолжение). Не выводи период из имени документа, текущей даты,
типичного отчётного цикла или собственных знаний. Если периода нет — верни null: система сама возьмёт
ВСЕ доступные периоды отчёта. Отсутствие периода — НЕ неоднозначность: никогда не добавляй его в
unresolved/ambiguities и не проси уточнить.

Закрытые перечисления (используй ТОЛЬКО эти значения, ничего своего):
- intent_kind — один из:
  "extract_registered_metric" (просят показать/сообщить значение одного или нескольких конкретно
    названных показателей — "какой/сколько/покажи/дай", — и показатель похож на что-то из
    кандидатов ниже),
  "extract_ad_hoc_metric" (то же самое действие — показать значение, — но названный показатель
    явно НЕ похож ни на один код из кандидатов ниже и не является стандартной строкой отчётности),
  "calculate_registered_metric" (явно просят ПОСЧИТАТЬ/РАССЧИТАТЬ/ВЫЧИСЛИТЬ показатель, и он похож
    на что-то из кандидатов ниже),
  "calculate_ad_hoc_metric" (просят посчитать/рассчитать показатель, которого нет среди кандидатов),
  "full_analysis" (запрошен широкий/полный разбор без перечисления конкретных показателей),
  "extract_structure" (запрошена структура/таблица раскрытия — сегменты, сроки погашения долга,
    состав группы, — а не один числовой показатель),
  "document_qa" (вопрос про содержимое документа — цитату, формулировку, условие договора, — а не
    про значение показателя),
  "compare_metrics" (явное сравнение конкретных показателей между периодами или компаниями),
  "explain_change" (вопрос "почему/за счёт чего изменился" НАЗВАННЫЙ показатель — ищут ПРИЧИНУ
    изменения ОДНОГО числа, не общую сводку различий),
  "validate_report" (запрошена сверка/проверка непротиворечивости данных внутри отчёта или между
    несколькими отчётами),
  "plan_fact_analysis" (сравнение фактических показателей с планом/бюджетом),
  "monitor_changes" (вопрос "что изменилось/что нового у эмитента" БЕЗ причинного "почему" и без
    одного названного показателя — общая сводка изменений показателей отчёта между отчётными
    периодами (текущий период против предыдущего) с объяснением, ИЛИ просьба следить за
    изменениями),
  "form" (запрошена конкретная форма/таблица/layout по роли заказчика),
  "custom_analysis" (просят ЛЮБУЮ обработку данных, которой нет среди видов выше: статистику —
    корреляцию, регрессию, тренд, волатильность; таблицу, график или расчёт собственной структуры;
    работу с внешними данными; выгрузку результата АНАЛИЗА в файл (Excel). targets пустые, текст
    анализа дословно положи в unresolved. Извлечение показателей, на которых строится анализ, можно
    вынести отдельной целью с depends_on (см. «СОСТАВНОЙ ЗАПРОС») или оставить в этой же цели — обе
    формы допустимы; анализ в любом случае не превращай в extract/form плюс остаток.
    Без АНАЛИЗА (статистики, собственного расчёта, внешних данных) это НЕ custom_analysis: просьба
    «извлеки/покажи показатели и верни таблицу в excel / выгрузи в файл» — это обычное извлечение
    (extract_registered_metric либо form), сам файл не делает её анализом; фразу про файл положи в
    unresolved. Примеры НЕ custom_analysis: «извлеки выручку и верни таблицу в excel»,
    «покажи EBITDA и выгрузи в Excel». Примеры custom_analysis: «извлеки показатели и посчитай
    корреляцию, результат верни в excel», «построй регрессию выручки на цене Brent»)

Разграничение "explain_change" от "monitor_changes" (частая путаница — оба про "изменения"):
  - "почему выручка упала?", "за счёт чего вырос долг?" → explain_change (назван ОДИН показатель,
    вопрос "почему").
  - "что изменилось у эмитента?", "что нового в отчёте по сравнению с прошлым?", "следи за
    изменениями по этой компании" → monitor_changes (показатель НЕ назван, вопрос "что", не
  "почему" — общий обзор различий, а не причина одного числа).

СРАВНЕНИЕ С ПРЕОБРАЗОВАНИЕМ ИЛИ ВНЕШНИМ РЯДОМ. Если пользователь просит сравнить названные
показатели после того, как их нужно нормировать или привести к базе 100, верни ОДНУ цель
`compare_metrics`, а не несколько `calculate_ad_hoc_metric`. Если просят сопоставить показатель
отчёта с внешним рядом или URL, также сохрани `compare_metrics`: внешнюю часть дословно положи в
`unresolved`, но не понижай цель до extract_registered_metric только потому, что внешний ряд не
является показателем отчётности.

- role: одно из other, risks, baifp, die, cib, dgr. Если роль выбрана в интерфейсе, верни именно её:
  UI-роль — источник истины для scope полного анализа и формы. Без UI-роли используй роль только если
  пользователь явно назвал роль/формат заказчика; иначе null.

«Извлеки все показатели» / «извлеки показатели из отчёта» — это form с role="other",
а НЕ extract_ad_hoc_metric: пользователь просит общую таблицу зарегистрированных показателей,
а не один новый показатель с названием «все показатели». Если период не назван, оставь
period_ref=null: канонический runtime сам построит таблицу по всем отчётным периодам документа.

Показатели (targets[].concept) — ТОЛЬКО коды из списка кандидатов ниже, и только когда
intent_kind — "extract_registered_metric" или "calculate_registered_metric". Если пользователь
назвал показатель, а среди кандидатов нет уверенного совпадения — НЕ придумывай код, вместо
этого добавь исходную фразу пользователя в unresolved (дословно, как её сказал пользователь) и
используй соответствующий "*_ad_hoc_metric" — независимо от глагола: для просьбы показать/дать/
извлечь значение ("покажи", "дай", "какой", "сколько", "извлеки") — "extract_ad_hoc_metric", для
"посчитай/рассчитай" — "calculate_ad_hoc_metric". Вид "extract_registered_metric" или
"calculate_registered_metric" с пустыми targets и непустым unresolved — ошибка: показатель без
кандидата ВСЕГДА получает "*_ad_hoc_metric". Если два и более кандидата одинаково подходят и
невозможно выбрать однозначно — добавь запись в ambiguities с term (что сказал пользователь) и
candidates (коды-кандидаты), а этот показатель НЕ добавляй в targets.

Широкая категория во множественном смысле — НЕ неоднозначность одного показателя. Например,
«извлеки прибыль» / «покажи показатели прибыли» означает выбрать ВСЕ подходящие кандидаты,
которые сами являются видами прибыли (валовая, от продаж, до налогообложения, чистая), и вернуть
их отдельными targets: дальше детерминированный runtime соберёт один scoped FormOutput. Не включай
связанные, но не являющиеся видами прибыли показатели (например EPS или рентабельность). Если же
формулировка требует ОДНО значение («какая прибыль?») и несколько видов подходят одинаково — это
настоящая ambiguities, её нужно вернуть для уточнения.

Для всех intent_kind, кроме "extract_registered_metric" и "calculate_registered_metric", список
targets ВСЕГДА пустой []: не перечисляй показатели, которые могли бы войти в полный анализ, форму,
структуру или другой составной результат — это решает не ты.

ПОКАЗАТЕЛЬ ВНЕ РЕЕСТРА — ИЗВЛЕЧЕНИЕ (intent_kind="extract_ad_hoc_metric"). Пользователь НЕ обязан
описывать показатель. Ты, опираясь на свои знания финансовой отчётности, сам придумываешь его
описание, по которому система найдёт строку в отчёте, и заполняешь ad_hoc_concept:
  name_ru, name_en — стандартное название;
  aliases_ru, aliases_en — 5–10 реальных вариантов формулировки в отчётности (включая типичные
    сокращения и английские термины);
  definition — что это за величина (одно-два предложения);
  hint — где и как искать в отчёте (какая форма или примечание, в каких единицах);
  value_kind — "flow" (за период), "stock" (на дату), "ratio" (отношение/процент) или "other".

РАСЧЁТ.
- "calculate_registered_metric": показатель есть среди кандидатов — заполни targets; формула берётся
  из реестра, свою не придумывай.
- "calculate_ad_hoc_metric": показателя нет в реестре. Пользователь формулу НЕ обязан называть: если
  он её не назвал, выдай формулу сам по общепринятой методике (выбранную трактовку укажи в
  assumptions). Заполни ad_hoc_formula:
    name_ru, name_en, definition — что считаем; unit — единицы результата ("%", "x", "RUB" ...);
    expression — арифметика ТОЛЬКО из чисел, круглых скобок и символов через + - * / (никаких
      функций, степеней, условий);
    components — ОБЪЕКТ «символ → название показателя на русском»: по одному на КАЖДЫЙ символ выражения
      (символ — ЗАГЛАВНЫМИ латинскими буквами, цифрами и _). Название пиши в стандартной формулировке
      отчётности; если такой показатель есть среди кандидатов ниже — ДОСЛОВНО как в списке кандидатов
      (тогда система сопоставит его с реестром). Описание понятий (синонимы, определение) не нужно —
      его система запросит отдельно;
    assumptions — принятые допущения (строка, может быть пустой).
  Каждый символ выражения обязан иметь компонент и наоборот; компоненты — ЗАПОЛНЕННЫЕ объекты (null и
  пустые объекты недопустимы). Если формулу нельзя свести к арифметике над показателями отчётности —
  оставь ad_hoc_formula null и назови причину в unresolved.
  Для промежуточного коэффициента покрытия / быстрой ликвидности по РСБУ используй устойчивую
  стандартную трактовку: (Итого оборотных активов - Запасы) / Итого краткосрочных обязательств.
  Не собирай числитель из торговой дебиторской задолженности: она уже общего итога строки 1230.
  ПРИМЕР ad_hoc_formula (запрос «посчитай оборачиваемость запасов»). Пример показывает ТОЛЬКО форму:
  name_en, definition, assumptions и components заполняй под запрос пользователя, не копируй из примера.
  "ad_hoc_formula": {"name_ru": "Оборачиваемость запасов", "name_en": "Inventory turnover",
    "definition": "Сколько раз за период запасы полностью реализуются: выручка, делённая на запасы.", "unit": "раз",
    "expression": "REV / INV",
    "assumptions": "Запасы берутся на конец периода.",
    "components": {"REV": "Выручка", "INV": "Запасы"}}

Для intent_kind="explain_change" отдельно заполни analysis_concepts — коды НАЗВАННЫХ
пользователем показателей ТОЛЬКО из списка кандидатов ниже. Это семантический scope объяснения,
а не targets скалярного извлечения. Широкая категория во множественном смысле включает все её
прямые виды: например «финансовые вложения» включает долгосрочные и краткосрочные финансовые
вложения, если оба кода есть среди кандидатов. Связанные, но не являющиеся видами категории
показатели не включай. Для остальных intent_kind верни analysis_concepts=[].

Если в запросе нет ни компании, ни конкретного показателя, или запрос использует местоимение/дейксис
без антецедента ("она", "их", "как обычно") и контекст диалога не помогает — считай неразрешённую
часть (компанию или сам показатель) отдельным пунктом ambiguities или unresolved, не додумывай
значение. Компанию не спрашивай, если документ прикреплён: он и задаёт компанию.

Если запрос вне области сценария (инвестиционный совет, прогноз котировок/курса, ESG-метрика,
любой показатель не из финансовой отчётности) — соответствующая часть просьбы идёт в unresolved
как есть, targets для неё не создаётся.

ЯВНАЯ ПОПРАВКА ПОЛЬЗОВАТЕЛЯ (Срез 10): если пользователь прямо исправляет ранее названное
значение (например «нет, EBITDA была 120», «исправь значение на 5 млн») или отменяет свою
предыдущую поправку, НЕ считай это новым независимым фактом. Заполни поле correction.
- action="apply" для новой поправки, action="cancel" для отмены/возврата к исходному.
- concept — код из кандидатов, только если однозначно распознан; иначе null.
- evidence_id — только если пользователь буквально назвал идентификатор доказательства; не выдумывай.
- value — число для apply, null для cancel.
- unit/period_ref/basis — только если явно названы пользователем.
- reason — явно названная причина, иначе пустая строка.
Точное связывание с существующим evidence делает Case после intent.parse; модель его не угадывает.

Верни СТРОГО JSON вида:
{
  "route": "plan" | "answer",
  "intent_kind": "extract_registered_metric" | "extract_ad_hoc_metric" | "calculate_registered_metric"
    | "calculate_ad_hoc_metric" | "full_analysis" | "extract_structure" | "document_qa"
    | "custom_analysis" | "compare_metrics" | "explain_change" | "validate_report" | "plan_fact_analysis"
    | "monitor_changes" | "form" | null,
  "targets": [{"concept": "ENUM_FROM_CANDIDATES"}, ...],
  "analysis_concepts": ["ENUM_FROM_CANDIDATES", ...],
  "ad_hoc_concept": null | {"name_ru": "...", "name_en": "...", "aliases_ru": ["..."], "aliases_en": ["..."],
    "definition": "...", "hint": "...", "value_kind": "flow|stock|ratio|other"},
  "ad_hoc_formula": null | {"name_ru": "...", "name_en": "...", "definition": "...", "unit": "%",
    "expression": "A / B * 100", "assumptions": "",
    "components": {"A": "название показателя A", "B": "название показателя B"}},
  "entity_ref": "название компании как в запросе, или null",
  "period_ref": "период, названный пользователем (например \\"2024\\", \\"Q1 2025\\"), или null",
  "role": "other|risks|baifp|die|cib|dgr, или null",
  "unresolved": ["фраза пользователя, которую не удалось сопоставить", ...],
  "ambiguities": [{"term": "...", "candidates": ["ENUM1", "ENUM2"], "reason": "..."}],
  "correction": null | {"action": "apply|cancel", "concept": "ENUM_FROM_CANDIDATES|null",
    "evidence_id": "literal evidence id|null", "value": 123.45, "unit": "RUB|null",
    "period_ref": "2024|null", "basis": "IFRS|null", "reason": "..."},
  "goal_ref": "goal-1",
  "depends_on": [],
  "additional_goals": [{"goal_ref": "goal-2", "depends_on": ["goal-1"],
    "intent_kind": "...", "targets": [], "analysis_concepts": [], "ad_hoc_concept": null,
    "ad_hoc_formula": null, "entity_ref": null, "period_ref": null, "role": null,
    "unresolved": [], "ambiguities": [], "correction": null}]
}
Никакого текста вне JSON."""

# Для вызовов без маршрутизации (станционный CLI `aifa-metric`): ответ «сам» невозможен.
_PLAN_ONLY_SUFFIX = (
    '\n\nВАЖНО: в этом вызове route ВСЕГДА "plan" — ответ «сам» не поддерживается; '
    "выбери наиболее подходящий intent_kind."
)

_COMPOUND_CLAUSE_REPAIR_SYSTEM = """Ты ремонтируешь РОВНО ОДНУ дополнительную цель составного
запроса финансового аналитика. Верни ТОЛЬКО полный JSON-объект этой цели, без route и
additional_goals:
{"goal_ref":"goal-2","depends_on":[],"intent_kind":"...","targets":[],
"analysis_concepts":[],"ad_hoc_concept":null,"ad_hoc_formula":null,"entity_ref":null,
"period_ref":null,"role":null,"unresolved":[],"ambiguities":[],"correction":null}.
Все поля-массивы ВСЕГДА являются массивами, не null. depends_on содержит goal_ref другой цели
ТОЛЬКО если текущий результат использует её результат; соседство в одном запросе не создаёт
зависимость. Сохрани intent_kind черновика, если он соответствует просьбе пользователя.

Для extract_registered_metric/calculate_registered_metric заполни targets только кодами из
переданных кандидатов. Для остальных intent_kind targets=[]. Для extract_ad_hoc_metric заполни
ad_hoc_concept (name_ru, name_en, aliases_ru, aliases_en, definition, hint, value_kind). Для
calculate_ad_hoc_metric заполни ad_hoc_formula: name_ru, name_en, definition, unit, expression
(только числа, круглые скобки и символы через + - * /), assumptions и components — объект
«СИМВОЛ → стандартное название показателя». null допустим только если арифметическая формула
невозможна, тогда конкретную причину запиши в unresolved.
Для промежуточного коэффициента покрытия / быстрой ликвидности по РСБУ используй
(Итого оборотных активов - Запасы) / Итого краткосрочных обязательств; не собирай числитель
из торговой дебиторской задолженности.

Если черновик имеет intent_kind=custom_analysis, СОХРАНИ его: статистика и обработка ряда значений
(коэффициент вариации, медиана, среднее, стандартное отклонение, корреляция, регрессия), а также
всё, что требует внешних данных, — это custom_analysis, а не calculate_ad_hoc_metric (формула из
+ - * / их не выражает). Для custom_analysis targets=[], ad_hoc_formula=null, а текст анализа
дословно положи в unresolved как МАССИВ из одной строки: ["<текст анализа>"]; depends_on — массив
с goal_ref цели, поверх результата которой анализ считается.

Узкоспециализированный код допустим только при таком же узком смысле в тексте. Например,
«уплаченные дивиденды» — DIVIDENDS; вариант для неконтролирующих долей выбирай только когда
пользователь прямо назвал неконтролирующие доли. Не заменяй общий показатель его частным подвидом
только потому, что оба есть среди кандидатов. Никакого текста вне JSON."""


def _build_user_prompt(
    raw_query: str, *, language: str, candidates: dict[str, dict],
    context: IngressContext | None = None,
) -> str:
    lang_note = f"Язык запроса: {language}." if language else ""
    parts = [f"Запрос пользователя: {raw_query!r}\n{lang_note}"]
    if context is not None:
        parts.append(_context_prompt_block(context))
    parts.append(
        f"Кандидаты показателей (единственные допустимые коды для targets[].concept):\n"
        f"{_candidate_prompt_block(candidates)}"
    )
    return "\n\n".join(parts)


def _additional_clause_needs_repair(item: object) -> bool:
    if not isinstance(item, dict):
        return True
    try:
        _validate_goal_shape(item, label="additional goal")
    except _SchemaError:
        return True
    deps = item.get("depends_on")
    return not isinstance(deps, list) or any(not isinstance(value, str) for value in deps)


async def _repair_additional_clauses(
    data: dict,
    *,
    raw_query: str,
    candidates: dict[str, dict],
    complete_fn: Callable[..., Awaitable[str]],
    model_version: str | None,
) -> dict | None:
    """Repair malformed nested clauses with one small model call per clause.

    The live model reliably filled the primary goal but emitted a schema-shaped
    placeholder full of ``null`` values for ``additional_goals``. Repeating the
    original large prompt reproduced the placeholder byte-for-byte. A focused
    call succeeds because it has one output object and no nested schema. The
    merge below is structural only: dependency and financial semantics still
    come from the model and pass the same deterministic validator afterwards.
    """
    additional = data.get("additional_goals")
    if not isinstance(additional, list) or not additional:
        return None
    broken = [index for index, item in enumerate(additional) if _additional_clause_needs_repair(item)]
    if not broken:
        return None

    repaired = dict(data)
    repaired_goals = list(additional)
    primary = {key: value for key, value in data.items() if key not in ("route", "additional_goals")}
    for index in broken:
        draft = additional[index]
        other_goals = [primary, *repaired_goals[:index]]
        prompt = (
            f"Полный запрос пользователя: {raw_query!r}\n"
            f"Исправь только дополнительную цель номер {index + 2}.\n"
            f"Её невалидный черновик: {json.dumps(draft, ensure_ascii=False)}\n"
            "Уже определённые цели (их не повторяй):\n"
            f"{json.dumps(other_goals, ensure_ascii=False)}\n\n"
            "Кандидаты показателей (единственные допустимые коды targets):\n"
            f"{_candidate_prompt_block(candidates)}"
        )
        raw = await complete_fn(_COMPOUND_CLAUSE_REPAIR_SYSTEM, prompt, model_version=model_version)
        try:
            clause = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _SchemaError(f"focused repair additional_goals[{index}] вернул не JSON: {exc}") from exc
        if not isinstance(clause, dict) or "route" in clause or "additional_goals" in clause:
            raise _SchemaError(
                f"focused repair additional_goals[{index}] должен вернуть один объект цели"
            )
        repaired_goals[index] = clause
    repaired["additional_goals"] = repaired_goals
    return repaired


def _explicit_numbered_clauses(raw_query: str) -> list[str]:
    """Return explicit ``1. ... 2. ...`` items without interpreting their meaning.

    This is deliberately only a structural parser.  It is used after a large
    compound JSON response was truncated: financial semantics still come from
    one focused model call per verbatim item.  Free-form prose is not split by
    punctuation because that would turn a formatting fallback into a second
    intent parser.
    """
    matches = list(re.finditer(r"(?m)^\s*(\d+)[.)]\s+", raw_query))
    if len(matches) < 2:
        return []
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, len(matches) + 1)):
        return []

    preamble = raw_query[:matches[0].start()].strip()
    segments = [
        raw_query[match.end():(matches[index + 1].start() if index + 1 < len(matches) else None)].strip()
        for index, match in enumerate(matches)
    ]
    # Instructions after the numbered list apply to every item.  The smoke
    # contract separates them from item 4 with a blank line, so preserve that
    # suffix verbatim instead of attaching it only to the last goal.
    shared_suffix = ""
    last_parts = re.split(r"\n\s*\n", segments[-1], maxsplit=1)
    if len(last_parts) == 2:
        segments[-1], shared_suffix = (part.strip() for part in last_parts)
    return ["\n\n".join(part for part in (preamble, segment, shared_suffix) if part) for segment in segments]


async def _parse_numbered_compound_after_truncation(
    *,
    raw_query: str,
    candidates: dict[str, dict],
    complete_fn: Callable[..., Awaitable[str]],
    model_version: str | None,
) -> dict | None:
    """Decompose an explicitly numbered request after the monolithic JSON was cut off."""
    clauses = _explicit_numbered_clauses(raw_query)
    if not clauses:
        return None

    def _focused_value_kind(text: str) -> str:
        normalized = " ".join(text.casefold().split())
        if re.search(r"\b(?:коэффициент|доля|рентабельност|margin|ratio|rate)\w*", normalized):
            return "ratio"
        if re.search(r"\b(?:балансов|остаток|на дату|актив|обязательств|equity|asset|liabilit)\w*", normalized):
            return "stock"
        if re.search(r"\b(?:выручк|доход|расход|прибыл|денежн.*поток|revenue|income|expense|cash flow)\w*", normalized):
            return "flow"
        return "other"

    async def _one(index: int, clause_text: str) -> dict:
        base_prompt = (
            f"Полный запрос пользователя: {raw_query!r}\n"
            f"Разбери только цель номер {index + 1}, дословный фрагмент:\n{clause_text}\n\n"
            "Кандидаты показателей (единственные допустимые коды targets):\n"
            f"{_candidate_prompt_block(candidates)}"
        )
        last_error: Exception | None = None
        # A focused clause is cheap compared with failing the whole compound
        # request.  Two repairs were not enough in practice for standard
        # formulas that the model occasionally rendered with prose/functions
        # in ``expression`` on both attempts.  Keep the retry local to this
        # one clause so already valid siblings are not parsed again.
        for attempt in range(3):
            prompt = base_prompt
            if last_error is not None:
                prompt += (
                    f"\n\nПредыдущий ответ этой цели не прошёл схему: {last_error}. "
                    "Исправь только указанную ошибку и верни полный JSON-объект цели."
                )
            raw = await complete_fn(
                _COMPOUND_CLAUSE_REPAIR_SYSTEM, prompt, model_version=model_version,
            )
            try:
                clause = json.loads(raw)
                if not isinstance(clause, dict) or "route" in clause or "additional_goals" in clause:
                    raise _SchemaError(f"цель {index + 1} должна быть одним JSON-объектом")
                raw_spec = clause.get("ad_hoc_concept")
                if isinstance(raw_spec, list) and len(raw_spec) == 1 and isinstance(raw_spec[0], dict):
                    clause["ad_hoc_concept"] = raw_spec = raw_spec[0]
                if (
                    clause.get("intent_kind") == "extract_ad_hoc_metric"
                    and isinstance(raw_spec, str) and raw_spec.strip()
                ):
                    clause["ad_hoc_concept"] = {
                        "name_ru": raw_spec.strip(), "name_en": "",
                        "aliases_ru": [], "aliases_en": [], "definition": "", "hint": "",
                        "value_kind": _focused_value_kind(f"{raw_spec} {clause_text}"),
                    }
                explicit_period = re.search(
                    r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}|(?:19|20)\d{2})(?!\d)",
                    clause_text,
                )
                if explicit_period and not str(clause.get("period_ref") or "").strip():
                    clause["period_ref"] = explicit_period.group(0)
                clause["goal_ref"] = f"goal-{index + 1}"
                _validate_goal_shape(clause, label=f"цель {index + 1}")
                _validate_focused_clause_semantics(
                    clause, candidates=candidates, label=f"цель {index + 1}",
                    clause_text=clause_text,
                )
                return clause
            except (json.JSONDecodeError, _SchemaError) as exc:
                last_error = exc
        raise _SchemaError(f"цель {index + 1} не прошла focused repair: {last_error}")

    parsed = await asyncio.gather(*(_one(index, text) for index, text in enumerate(clauses)))
    primary = dict(parsed[0])
    primary["route"] = "plan"
    primary["additional_goals"] = parsed[1:]
    return primary


_AD_HOC_GOAL_SET_REPAIR_SYSTEM = """Ты исправляешь неполный разбор запроса о показателях вне реестра.
Верни СТРОГО JSON {"goals": [...]}: один самостоятельный объект цели на КАЖДЫЙ явно
запрошенный пользователем показатель. Не объединяй разные статьи в одну цель и не теряй их.
Каждый объект обязан иметь intent_kind, targets=[], analysis_concepts=[], entity_ref, period_ref,
role, unresolved=[], ambiguities=[], depends_on=[] и ad_hoc_concept. В ad_hoc_concept заполни
name_ru, name_en, aliases_ru, aliases_en, definition, hint и value_kind. value_kind="stock" для
остатка на дату, "flow" для суммы за период, "ratio" для отношения; "other" только если величина
действительно не относится ни к одному из этих типов. period_ref задавай отдельно для каждой
статьи: дата отчёта относится к stock, «за полугодие» — к flow; не копируй одну дату во все цели.
Никакого текста вне JSON."""


def _has_underspecified_ad_hoc_goal(data: dict, raw_query: str) -> bool:
    for clause in (data, *(data.get("additional_goals") or [])):
        if not isinstance(clause, dict) or clause.get("intent_kind") != "extract_ad_hoc_metric":
            continue
        spec = clause.get("ad_hoc_concept")
        if spec is None:
            return len(" ".join(raw_query.split())) > 160
        if not isinstance(spec, dict):
            return True
        name = str(spec.get("name_ru") or spec.get("name_en") or "").strip()
        term = str(spec.get("term") or "").strip()
        compact_query = " ".join(raw_query.split()).casefold()
        compact_name = " ".join(name.split()).casefold()
        if (
            not name
            or term == raw_query.strip()
            or compact_name == compact_query
            or len(compact_name) > 160
        ):
            return True
    return False


async def _repair_underspecified_ad_hoc_goal_set(
    data: dict,
    *,
    raw_query: str,
    candidates: dict[str, dict],
    complete_fn: Callable[..., Awaitable[str]],
    model_version: str | None,
) -> dict | None:
    """Ask the model for the complete atomic goal set when a valid JSON placeholder hid omissions."""
    if not _has_underspecified_ad_hoc_goal(data, raw_query):
        return None
    base_prompt = (
        f"Полный запрос пользователя: {raw_query!r}\n\n"
        "Кандидаты реестра (не подменяй ими явно названные ad-hoc статьи):\n"
        f"{_candidate_prompt_block(candidates)}"
    )
    last_error: Exception | None = None
    for _attempt in range(2):
        prompt = base_prompt
        if last_error is not None:
            prompt += f"\n\nПредыдущая попытка не прошла схему: {last_error}. Исправь её."
        raw = await complete_fn(_AD_HOC_GOAL_SET_REPAIR_SYSTEM, prompt, model_version=model_version)
        try:
            payload = json.loads(raw)
            goals = payload.get("goals") if isinstance(payload, dict) else None
            if not isinstance(goals, list) or not goals:
                raise _SchemaError("focused ad-hoc repair должен вернуть непустой goals[]")
            for index, clause in enumerate(goals, start=1):
                if not isinstance(clause, dict):
                    raise _SchemaError(f"goals[{index - 1}] не объект")
                for field in ("targets", "analysis_concepts", "unresolved", "ambiguities", "depends_on"):
                    if clause.get(field) is None:
                        clause[field] = []
                clause["goal_ref"] = f"goal-{index}"
                try:
                    _validate_goal_shape(clause, label=f"goals[{index - 1}]")
                except _SchemaError as shape_error:
                    focused_prompt = (
                        f"Полный запрос пользователя: {raw_query!r}\n"
                        f"Исправь только цель {index}; её черновик:\n"
                        f"{json.dumps(clause, ensure_ascii=False)}\n\n"
                        f"Ошибка схемы: {shape_error}.\n"
                        "Верни один полный JSON-объект цели, без route и additional_goals.\n\n"
                        "Кандидаты показателей:\n"
                        f"{_candidate_prompt_block(candidates)}"
                    )
                    focused_raw = await complete_fn(
                        _COMPOUND_CLAUSE_REPAIR_SYSTEM,
                        focused_prompt,
                        model_version=model_version,
                    )
                    try:
                        focused = json.loads(focused_raw)
                    except json.JSONDecodeError as exc:
                        raise _SchemaError(
                            f"focused repair goals[{index - 1}] вернул не JSON: {exc}"
                        ) from exc
                    if not isinstance(focused, dict) or "route" in focused or "additional_goals" in focused:
                        raise _SchemaError(
                            f"focused repair goals[{index - 1}] должен вернуть один объект цели"
                        )
                    clause = focused
                    goals[index - 1] = clause
                    for field in ("targets", "analysis_concepts", "unresolved", "ambiguities", "depends_on"):
                        if clause.get(field) is None:
                            clause[field] = []
                    clause["goal_ref"] = f"goal-{index}"
                    _validate_goal_shape(clause, label=f"goals[{index - 1}]")
                spec = clause.get("ad_hoc_concept")
                name = str((spec or {}).get("name_ru") or (spec or {}).get("name_en") or "").strip()
                kind = str((spec or {}).get("value_kind") or "").strip().lower()
                if clause.get("intent_kind") != "extract_ad_hoc_metric" or not name:
                    raise _SchemaError(
                        f"goals[{index - 1}] должен описывать один именованный extract_ad_hoc_metric"
                    )
                compact_name = " ".join(name.split()).casefold()
                if compact_name == " ".join(raw_query.split()).casefold() or len(compact_name) > 160:
                    raise _SchemaError(
                        f"goals[{index - 1}].ad_hoc_concept.name_ru копирует весь запрос; "
                        "верни только краткое название одной статьи"
                    )
                if kind not in {"stock", "flow", "ratio"}:
                    raise _SchemaError(
                        f"goals[{index - 1}].ad_hoc_concept.value_kind={kind!r}; "
                        "для числовой статьи выбери её временную ось stock, flow или ratio"
                    )
                half_year = re.search(
                    r"(?:полугоди\w*|6\s+месяц\w*)[^\d]{0,30}((?:19|20)\d{2})?",
                    raw_query,
                    flags=re.IGNORECASE,
                )
                if kind == "flow" and half_year:
                    year = half_year.group(1)
                    if not year:
                        years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", raw_query)
                        year = years[-1] if years else ""
                    if year:
                        clause["period_ref"] = f"6M {year}"
            primary = dict(goals[0])
            primary["route"] = "plan"
            primary["additional_goals"] = goals[1:]
            return primary
        except (json.JSONDecodeError, _SchemaError) as exc:
            last_error = exc
    raise _SchemaError(f"focused ad-hoc goal-set repair не прошёл схему: {last_error}")


async def _default_complete(system: str, prompt: str, *, model_version: str | None) -> str:
    from finengine.llm import openrouter as _llm  # noqa: PLC0415 — тот же единый шлюз, что у two_stage_extractor
    from finengine.skills.spec.atom_author import without_markdown_fence  # noqa: PLC0415 — ограда GLM, ADR-0048

    return without_markdown_fence(await asyncio.wait_for(
        _llm.complete(
            prompt, model=model_version, system=system, response_format={"type": "json_object"},
            temperature=0.0, retries=1, max_output=6000, reasoning_effort="low",
        ),
        timeout=settings.routing_model_timeout_seconds,
    ))


# ---------------------------------------------------------------------------
# Детерминированная проверка ответа модели — «После модельного ответа
# детерминированная проверка подтверждает существование концептов,
# документов, ролей и периодов» (карточка).
class _SchemaError(ValueError):
    """Внутренний сигнал: ответ модели не прошёл проверку СТРУКТУРЫ (не
    содержимого) — повод для единственной повторной попытки.

    `data` — уже разобранный JSON ответа, если отказ вызвала только невалидная
    формула: после исчерпания попыток цель строится без формулы (информативный
    отказ «модель не составила проверяемую формулу»), а не общий отказ разбора."""

    data: dict | None = None


def _parse_and_validate_shape(raw: str, *, allow_answer: bool = True) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _SchemaError(f"ответ модели не является JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _SchemaError("ответ модели — не JSON-объект")
    route = data.get("route", "plan")
    if route not in ("plan", "answer"):
        raise _SchemaError(f"route={route!r} должен быть plan|answer")
    if route == "answer":
        if not allow_answer:
            raise _SchemaError('route="answer" недопустим в этом вызове — верни route="plan"')
        # Ответ «сам»: намерение не нужно, остальные поля модели игнорируются.
        return data
    _validate_goal_shape(data, label="основная цель")
    additional = data.get("additional_goals", [])
    if not isinstance(additional, list):
        raise _SchemaError("additional_goals не список")
    refs = [str(data.get("goal_ref") or "goal-1")]
    for index, item in enumerate(additional, start=2):
        if not isinstance(item, dict):
            raise _SchemaError(f"additional_goals[{index - 2}] не объект")
        _validate_goal_shape(item, label=f"additional_goals[{index - 2}]")
        ref = str(item.get("goal_ref") or f"goal-{index}").strip()
        if not ref or ref in refs:
            raise _SchemaError(f"goal_ref {ref!r} пуст или повторяется")
        refs.append(ref)
    known = set(refs)
    for index, item in enumerate([data, *additional]):
        deps = item.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(v, str) for v in deps):
            raise _SchemaError(f"depends_on цели {refs[index]!r} должен быть списком строк")
        if refs[index] in deps or any(dep not in known for dep in deps):
            raise _SchemaError(f"depends_on цели {refs[index]!r} содержит неизвестную цель или саму себя")
    return data


def _validate_goal_shape(data: dict, *, label: str = "цель") -> None:
    """Validate one goal clause; compound topology is checked by the caller."""
    if data.get("intent_kind") not in _INTENT_KINDS:
        raise _SchemaError(f"{label}: intent_kind={data.get('intent_kind')!r} не входит в {_INTENT_KINDS}")
    if not isinstance(data.get("targets"), list):
        raise _SchemaError(f"{label}.targets отсутствует или не список (null недопустим, используй [])")
    # Models often collapse the one-field object to its scalar value.  This
    # representation is unambiguous and still goes through the closed-catalog
    # semantic check below, so repair it deterministically instead of spending
    # several model retries on JSON ceremony.
    data["targets"] = [
        {"concept": target.strip()} if isinstance(target, str) and target.strip() else target
        for target in data["targets"]
    ]
    if any(not isinstance(target, dict) for target in data.get("targets") or []):
        raise _SchemaError(
            f"{label}.targets должен содержать объекты вида {{\"concept\": \"CODE\"}}, не строки"
        )
    if not isinstance(data.get("unresolved", []), list):
        raise _SchemaError(f"{label}.unresolved не список (null недопустим, используй [])")
    if not isinstance(data.get("ambiguities", []), list):
        raise _SchemaError(f"{label}.ambiguities не список (null недопустим, используй [])")
    if not isinstance(data.get("analysis_concepts", []), list):
        raise _SchemaError(f"{label}.analysis_concepts не список (null недопустим, используй [])")
    ad_hoc_concept = data.get("ad_hoc_concept")
    if ad_hoc_concept is not None and not isinstance(ad_hoc_concept, dict):
        raise _SchemaError("ad_hoc_concept не объект/null")
    ad_hoc_formula = data.get("ad_hoc_formula")
    if (
        data.get("intent_kind") == "calculate_ad_hoc_metric"
        and ad_hoc_formula is None
        and not data.get("unresolved")
    ):
        raise _SchemaError(
            f"{label}.ad_hoc_formula=null допустима только с конкретной причиной в unresolved"
        )
    if ad_hoc_formula is not None:
        if not isinstance(ad_hoc_formula, dict):
            raise _SchemaError("ad_hoc_formula не объект/null")
        for field_name, allowed in (("dependencies", (list,)), ("components", (list, dict))):
            value = ad_hoc_formula.get(field_name)
            if value is not None and not isinstance(value, allowed):
                raise _SchemaError(
                    f"ad_hoc_formula.{field_name} должен быть "
                    + ("объектом «символ → название»" if field_name == "components" else "списком")
                )
        if data.get("intent_kind") == "calculate_ad_hoc_metric":
            # Живая проверка (M2, dev): модель вернула `components: [null, null]`.
            # Невалидная формула — невалидная схема: модели возвращается КОНКРЕТНАЯ
            # причина, и она получает одну повторную попытку (честный `null` —
            # осознанный отказ модели — сюда не попадает).
            reason = formula_rejection_reason(ad_hoc_formula)
            if reason:
                error = _SchemaError(f"ad_hoc_formula отвергнута: {reason}")
                error.data = data
                raise error
    correction = data.get("correction")
    if correction is not None:
        if not isinstance(correction, dict):
            raise _SchemaError("correction не объект/null")
        if correction.get("action", "apply") not in ("apply", "cancel"):
            raise _SchemaError("correction.action должен быть apply|cancel")
        value = correction.get("value")
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise _SchemaError("correction.value должен быть числом/null")


def _validate_focused_clause_semantics(
    data: dict, *, candidates: dict[str, dict], label: str, clause_text: str = "",
) -> None:
    """Reject schema-shaped placeholders before a focused clause is accepted.

    The ordinary intent path may deliberately preserve an unresolved registered
    request as an empty target list. A numbered fallback has one explicit result
    per clause, however, so accepting an empty registered target silently drops
    that result from the graph. Likewise, ``value_kind=other`` loses the period
    contract for a financial amount that the focused call is expected to define.
    """
    intent_kind = data.get("intent_kind")
    if intent_kind in ("extract_registered_metric", "calculate_registered_metric"):
        targets = data.get("targets") or []
        if not re.search(r"неконтролирующ|non[- ]controlling|\bNCI\b", clause_text, re.I):
            for target in targets:
                if (
                    str(target.get("concept") or "").strip().upper()
                    == "DIVIDENDS_PAID_NONCONTROLLING_INTERESTS"
                    and "DIVIDENDS" in candidates
                ):
                    target["concept"] = "DIVIDENDS"
        if not targets:
            raise _SchemaError(f"{label}.targets пуст: выбери код зарегистрированного показателя")
        unknown = [
            str(target.get("concept") or "")
            for target in targets
            if str(target.get("concept") or "") not in candidates
        ]
        if unknown:
            raise _SchemaError(
                f"{label}.targets содержит коды не из кандидатов: {', '.join(unknown)}"
            )
    if intent_kind == "extract_ad_hoc_metric":
        spec = data.get("ad_hoc_concept")
        name = str((spec or {}).get("name_ru") or (spec or {}).get("name_en") or "").strip()
        value_kind = str((spec or {}).get("value_kind") or "").strip()
        if not name:
            raise _SchemaError(f"{label}.ad_hoc_concept не содержит название показателя")
        if len(" ".join(name.split())) > 160:
            raise _SchemaError(
                f"{label}.ad_hoc_concept.name_ru копирует весь запрос; "
                "верни только краткое название одной статьи"
            )
        if value_kind not in ("stock", "flow", "ratio"):
            raise _SchemaError(
                f"{label}.ad_hoc_concept.value_kind должен быть stock|flow|ratio, не {value_kind!r}"
            )
    explicit_period = re.search(
        r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}|(?:19|20)\d{2})(?!\d)",
        clause_text,
    )
    if explicit_period and not str(data.get("period_ref") or "").strip():
        raise _SchemaError(
            f"{label}.period_ref пуст, хотя во фрагменте явно назван период {explicit_period.group(0)!r}"
        )


def _finalize_goal(
    data: dict, *, raw_query: str, candidates: dict[str, dict], ui_role: str = "",
) -> GoalIR:
    """Вторая половина «детерминированной проверки»: то, что прошло
    структурную валидацию выше, здесь проверяется ПО СОДЕРЖИМОМУ — концепт
    реально среди кандидатов, роль реально из закрытого списка. Ничего из
    этой функции не может провалиться исключением: несуществующее значение
    просто не становится `TargetNode`/`role` и уходит в `unresolved`, как и
    требует карточка («неизвестный концепт... попадает в unresolved»).

    Срез 31: выбранная пользователем роль интерфейса (`ui_role`) — источник
    истины и побеждает модельную догадку из текста. Только если UI-роли нет,
    принимается валидная явно названная роль из ответа модели."""
    intent_kind = data["intent_kind"]
    unresolved: list[str] = [str(u) for u in data.get("unresolved") or [] if str(u).strip()]
    ambiguities: list[AmbiguityNote] = []
    for item in data.get("ambiguities") or []:
        if not isinstance(item, dict) or not str(item.get("term") or "").strip():
            continue
        cands = [str(c) for c in (item.get("candidates") or []) if str(c).strip()]
        ambiguities.append(AmbiguityNote(
            term=str(item["term"]).strip(), candidates=cands, reason=str(item.get("reason") or ""),
        ))

    targets: list[TargetNode] = []
    if intent_kind in _METRIC_KINDS_WITH_TARGETS:
        for raw_target in data.get("targets") or []:
            concept = str((raw_target or {}).get("concept") or "").strip().upper()
            if concept and concept in candidates:
                targets.append(TargetNode(concept=concept, output_kind="scalar"))
            elif concept:
                # Модель выбрала код, которого не было среди кандидатов —
                # ровно тот случай, для которого нужна проверка ПОСЛЕ ответа,
                # не только промпт: закрытый список в промпте — не гарантия.
                unresolved.append(concept)
    # Все остальные intent_kind (включая *_ad_hoc_metric, где концепт по
    # определению не среди кандидатов) оставляют targets пустым независимо от
    # того, что прислала модель (A1.5) — она вообще не должна была их
    # присылать, но проверка на этом не останавливается на честном слове
    # промпта.

    selected_role = _ui_role_code(ui_role) or None
    model_role = str(data.get("role") or "").strip().lower() or None
    if selected_role is not None:
        role = selected_role
    else:
        role = model_role
        if role is not None and role not in _ROLES:
            unresolved.append(f"role:{role}")
            role = None
    if intent_kind == "form" and role is None and not any(a.term.startswith("role") for a in ambiguities):
        ambiguities.append(AmbiguityNote(
            term="форма/роль", candidates=list(_ROLES), reason="запрошена форма, но роль/layout не определены",
        ))

    entity_ref = str(data.get("entity_ref") or "").strip() or None
    period_ref = str(data.get("period_ref") or "").strip() or None

    correction = None
    raw_correction = data.get("correction")
    if isinstance(raw_correction, dict):
        corr_concept = str(raw_correction.get("concept") or "").strip().upper() or None
        if corr_concept is not None and corr_concept not in candidates:
            # Не превращаем придуманную моделью метку в identity поправки. Case
            # сможет разрешить её по остальным полям либо вернёт ChoicePoint.
            unresolved.append(corr_concept)
            corr_concept = None
        correction = CorrectionIntent(
            action=raw_correction.get("action") or "apply",
            concept=corr_concept,
            evidence_id=str(raw_correction.get("evidence_id") or "").strip() or None,
            value=raw_correction.get("value"),
            unit=str(raw_correction.get("unit") or "").strip() or None,
            period_ref=str(raw_correction.get("period_ref") or "").strip() or None,
            basis=str(raw_correction.get("basis") or "").strip() or None,
            reason=str(raw_correction.get("reason") or "").strip(),
            # Аудит хранит буквальную формулировку пользователя, а не
            # потенциальный пересказ модели.
            original_wording=raw_query,
        )

    parameters: dict = {}
    if intent_kind == "explain_change":
        analysis_concepts: list[str] = []
        for raw_concept in data.get("analysis_concepts") or []:
            concept = str(raw_concept or "").strip().upper()
            if not concept:
                continue
            if concept in candidates:
                if concept not in analysis_concepts:
                    analysis_concepts.append(concept)
            else:
                unresolved.append(concept)
        if analysis_concepts:
            parameters["analysis_concepts"] = analysis_concepts
    term_phrase = unresolved[0] if unresolved else raw_query
    if intent_kind == "extract_ad_hoc_metric":
        # Срез 31 (П5): имя, синонимы и определение придумывает модель. Если
        # она ничего не прислала, остаётся минимальная спецификация по фразе
        # пользователя — поведение прежнее, но не хуже.
        spec = concept_spec_from_payload(data.get("ad_hoc_concept"), fallback_term=term_phrase)
        parameters["ad_hoc_concept"] = (
            spec or AdHocConceptSpec(term=term_phrase)
        ).model_dump(mode="json")
    if intent_kind == "calculate_ad_hoc_metric":
        # Срез 31 (П4): формулу и её состав выдаёт модель, пользователь формулу
        # называть не обязан. Детерминированная проверка ПОСЛЕ ответа — в
        # `formula_spec_from_payload`: безопасное выражение, код реестра по
        # реальным реестрам, каждый символ описан компонентом. Недоверенная
        # формула отвергается ЦЕЛИКОМ (частично очищенная осталась бы со
        # свободной переменной).
        formula = formula_spec_from_payload(data.get("ad_hoc_formula"), fallback_term=term_phrase)
        if formula is not None:
            parameters["ad_hoc_formula"] = formula.model_dump(mode="json")

    additional_goals: list[GoalClause] = []
    for index, raw_goal in enumerate(data.get("additional_goals") or [], start=2):
        child_data = {**raw_goal, "additional_goals": []}
        child = _finalize_goal(
            child_data, raw_query=raw_query, candidates=candidates, ui_role=ui_role,
        )
        additional_goals.append(GoalClause(
            goal_ref=str(raw_goal.get("goal_ref") or f"goal-{index}"), raw_query=raw_query,
            intent_kind=child.intent_kind, targets=child.targets, entity_ref=child.entity_ref,
            period_ref=child.period_ref, role=child.role, parameters=child.parameters,
            unresolved=child.unresolved, ambiguities=child.ambiguities,
            depends_on=list(raw_goal.get("depends_on") or []),
        ))

    return GoalIR(
        raw_query=raw_query, intent_kind=intent_kind, targets=targets,
        entity_ref=entity_ref, period_ref=period_ref, role=role,
        parameters=parameters,
        unresolved=list(dict.fromkeys(unresolved)), ambiguities=ambiguities,
        correction=correction, goal_ref=str(data.get("goal_ref") or "goal-1"),
        depends_on=list(data.get("depends_on") or []), additional_goals=additional_goals,
    )


def _period_ref_is_grounded(period_ref: str, raw_query: str) -> bool:
    """Reject a model-invented concrete period on an initial turn.

    This is a grounding check, not period resolution: it never maps a user
    phrase to another label.  ``raw_query`` here is the current message plus the
    user's last replies, so a period carried over by a short follow-up
    («а за 2025?» after «выручка») stays grounded.
    """
    period_years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", period_ref))
    query_years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", raw_query))
    if period_years:
        if period_years.issubset(query_years):
            return True
        return bool(re.search(
            r"\b(?:прошл\w*|предыдущ\w*|текущ\w*|последн\w*|"
            r"previous|last|current)\b",
            raw_query,
            re.I,
        ))
    return period_ref.casefold() in raw_query.casefold()


# ---------------------------------------------------------------------------
# Описание компонентов формулы, которых нет в реестре (Срез 31, П4/П5). Ответ
# модели с самой формулой остаётся ПРОСТЫМ (символ → название) — живая проверка
# (M2, dev) показала, что вложенный массив объектов внутри большой схемы реальная
# модель разваливает. Синонимы, определение и подсказку для каждого такого
# понятия добирает небольшой отдельный вызов — тот же принцип, что для ad-hoc
# показателя. Сбой вызова разбор не ломает: понятие ищется по названию.
_DESCRIBE_SYSTEM = """Ты — эксперт по финансовой отчётности. Задача: описание понятий, по которому система найдёт строку в отчёте.
Для КАЖДОГО названия из запроса верни описание: name_ru (ДОСЛОВНО как получил), name_en,
aliases_ru и aliases_en (5–10 реальных формулировок в отчётности, включая типичные сокращения),
definition (что это за величина, одно-два предложения), hint (где и как искать: форма или
примечание отчёта, единицы), value_kind ("flow" — за период, "stock" — на дату, "ratio" — отношение, "other").
Верни СТРОГО JSON: {"concepts": [{"name_ru": "...", "name_en": "...", "aliases_ru": ["..."],
"aliases_en": ["..."], "definition": "...", "hint": "...", "value_kind": "flow|stock|ratio|other"}]}
Никакого текста вне JSON."""


def _parse_described(raw: str) -> list[dict]:
    data = json.loads(raw)
    items = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("concepts отсутствует или не список")
    return [item for item in items if isinstance(item, dict)]


async def _describe_formula_components(
    decision: "IntentDecision", complete_fn: Callable[..., Awaitable[str]], model_version: str | None,
) -> "IntentDecision":
    goal = decision.goal
    raw_formula = goal.parameters.get("ad_hoc_formula") if goal is not None else None
    if not isinstance(raw_formula, dict):
        return decision
    formula = AdHocFormulaSpec.model_validate(raw_formula)
    needy = [c for c in formula.components if needs_description(c)]
    if not needy:
        return decision
    names = [c.name_ru or c.term for c in needy]
    prompt = (
        f"Показатель «{formula.term}» = {formula.expression}.\nОпиши понятия:\n"
        + "\n".join(f"- {name}" for name in names)
    )
    try:
        described = _parse_described(await complete_fn(_DESCRIBE_SYSTEM, prompt, model_version=model_version))
    except Exception:  # noqa: BLE001 — сбой описания не должен ронять разбор
        return decision
    by_name = {" ".join(str(d.get("name_ru") or "").split()).casefold(): d for d in described}
    enriched: dict[str, AdHocConceptSpec] = {}
    for index, component in enumerate(needy):
        payload = by_name.get(names[index].casefold())
        if payload is None and len(described) == len(needy):
            payload = described[index]  # модель могла слегка переписать название — тогда по порядку
        if payload is None:
            continue
        described_spec = concept_spec_from_payload(
            {**payload, "name_ru": component.name_ru or component.term}, fallback_term=component.term,
        )
        if described_spec is not None:
            enriched[component.concept_key] = component.model_copy(update={
                "name_en": described_spec.name_en, "aliases_ru": described_spec.aliases_ru,
                "aliases_en": described_spec.aliases_en, "definition": described_spec.definition,
                "hint": described_spec.hint, "value_kind": described_spec.value_kind,
            })
    if not enriched:
        return decision
    updated = formula.model_copy(update={
        "components": [enriched.get(c.concept_key, c) for c in formula.components],
    })
    goal = goal.model_copy(update={"parameters": {**goal.parameters, "ad_hoc_formula": updated.model_dump(mode="json")}})
    return decision._replace(goal=goal)


# ---------------------------------------------------------------------------
async def parse_message(
    raw_query: str,
    *,
    context: IngressContext | None = None,
    language: str = "",
    model_version: str | None = None,
    complete_fn: Callable[..., Awaitable[str]] | None = None,
    allow_answer: bool = True,
) -> IntentDecision:
    """`intent.parse` как маршрутизатор: КАЖДОЕ сообщение → `IntentDecision`.

    Модель решает `plan` (запрос — один из GoalType, `goal` уходит планировщику)
    или `answer` (ответить самой по контексту). `allow_answer=False` — для вызовов,
    где ответа «сам» быть не может (станционный CLI): `route="answer"` там —
    невалидная схема, как любая другая.

    `complete_fn(system, prompt, *, model_version=...)` — точка подмены для
    тестов (единственная граница с живым модельным шлюзом). По умолчанию —
    реальный вызов через `finengine.llm.openrouter.complete` (единый шлюз,
    методология C4). Невалидный ответ повторяется ОДИН раз, затем
    `IntentParseError` — без «починки» скрытой эвристикой.

    Роль из интерфейса (`context.ui_role`) — источник истины для роли цели;
    модель не может заменить её догадкой из текста. Пустой `period_ref` цели
    означает «все доступные периоды» — период у пользователя не спрашивается.
    """
    query = (raw_query or "").strip()
    if not query:
        raise ValueError("parse_message: пустой raw_query")

    complete_fn = complete_fn or _default_complete
    candidates = _context_candidates(query, context)
    user_prompt = _build_user_prompt(query, language=language, candidates=candidates, context=context)
    system = _SYSTEM_PROMPT if allow_answer else _SYSTEM_PROMPT + _PLAN_ONLY_SUFFIX
    # Период считается «названным пользователем», если он есть в текущем
    # сообщении или в его последних репликах (короткое «а за 2025?»).
    grounding_text = " ".join([query, *_previous_user_turns(context)])

    def _decide(data: dict) -> IntentDecision:
        if data.get("route", "plan") == "answer":
            return IntentDecision(route="answer", goal=None, reason=str(data.get("reason") or ""))
        goal = _finalize_goal(
            data, raw_query=query, candidates=candidates, ui_role=context.ui_role if context else "",
        )
        if goal.period_ref and not _period_ref_is_grounded(goal.period_ref, grounding_text):
            goal = goal.model_copy(update={"period_ref": None})
        return IntentDecision(route="plan", goal=goal, reason=str(data.get("reason") or ""))

    last_error: Exception | None = None
    formula_only_data: dict | None = None
    for attempt in range(_MAX_RETRIES + 1):
        prompt = user_prompt
        if attempt > 0:
            prompt = (
                f"{user_prompt}\n\nПредыдущий ответ был невалиден: {last_error}. "
                "Верни СТРОГО валидный JSON по схеме. Перепроверь КАЖДУЮ цель, включая каждый "
                "объект additional_goals: targets, analysis_concepts, unresolved, ambiguities и "
                "depends_on обязаны быть массивами (пустое значение — [], не null). Для "
                "calculate_ad_hoc_metric заполни ad_hoc_formula; null допустим только вместе с "
                "конкретной причиной в unresolved."
            )
        raw = await complete_fn(system, prompt, model_version=model_version)
        try:
            data = _parse_and_validate_shape(raw, allow_answer=allow_answer)
        except _SchemaError as exc:
            # A repeated large prompt does not repair DeepSeek's nested
            # ``additional_goals`` placeholders. Spend the single retry on a
            # small one-clause contract instead, then validate the merged JSON
            # exactly like a first-pass response. No dependency or formula is
            # inferred by code.
            if attempt == 0:
                if raw.lstrip().startswith("{"):
                    try:
                        decomposed = await _parse_numbered_compound_after_truncation(
                            raw_query=query,
                            candidates=candidates,
                            complete_fn=complete_fn,
                            model_version=model_version,
                        )
                        if decomposed is not None:
                            data = _parse_and_validate_shape(
                                json.dumps(decomposed, ensure_ascii=False), allow_answer=allow_answer,
                            )
                            return await _describe_formula_components(
                                _decide(data), complete_fn, model_version,
                            )
                    except _SchemaError as repair_exc:
                        raise IntentParseError(
                            f"intent.parse: декомпозиция составной цели не прошла схему: {repair_exc}"
                        ) from repair_exc
                try:
                    raw_data = json.loads(raw)
                except json.JSONDecodeError:
                    raw_data = None
                if isinstance(raw_data, dict):
                    try:
                        repaired = await _repair_additional_clauses(
                            raw_data,
                            raw_query=query,
                            candidates=candidates,
                            complete_fn=complete_fn,
                            model_version=model_version,
                        )
                        if repaired is not None:
                            data = _parse_and_validate_shape(
                                json.dumps(repaired, ensure_ascii=False), allow_answer=allow_answer,
                            )
                            repaired_goal_set = await _repair_underspecified_ad_hoc_goal_set(
                                data,
                                raw_query=query,
                                candidates=candidates,
                                complete_fn=complete_fn,
                                model_version=model_version,
                            )
                            if repaired_goal_set is not None:
                                data = _parse_and_validate_shape(
                                    json.dumps(repaired_goal_set, ensure_ascii=False),
                                    allow_answer=allow_answer,
                                )
                            return await _describe_formula_components(_decide(data), complete_fn, model_version)
                    except _SchemaError as repair_exc:
                        raise IntentParseError(
                            f"intent.parse: focused repair составной цели не прошёл схему: {repair_exc}"
                        ) from repair_exc
            last_error = exc
            formula_only_data = exc.data
            continue
        try:
            repaired_goal_set = await _repair_underspecified_ad_hoc_goal_set(
                data,
                raw_query=query,
                candidates=candidates,
                complete_fn=complete_fn,
                model_version=model_version,
            )
            if repaired_goal_set is not None:
                data = _parse_and_validate_shape(
                    json.dumps(repaired_goal_set, ensure_ascii=False), allow_answer=allow_answer,
                )
        except _SchemaError as repair_exc:
            raise IntentParseError(
                f"intent.parse: focused repair неполного ad-hoc набора не прошёл схему: {repair_exc}"
            ) from repair_exc
        return await _describe_formula_components(_decide(data), complete_fn, model_version)

    if formula_only_data is not None:
        # Отказ был ТОЛЬКО из-за невалидной формулы, и модель упорствует: цель
        # строится без формулы (`_finalize_goal` её отбросит), а `root_from_goal`
        # честно скажет, что проверяемой формулы нет.
        return _decide(formula_only_data)

    raise IntentParseError(
        f"intent.parse: ответ модели не прошёл схему после {_MAX_RETRIES + 1} попыток: {last_error}"
    )


async def parse_intent(
    raw_query: str,
    *,
    language: str = "",
    model_version: str | None = None,
    complete_fn: Callable[..., Awaitable[str]] | None = None,
) -> GoalIR:
    """`intent.parse` без маршрутизации: свободный запрос → `GoalIR`.

    Тонкая обёртка над `parse_message(allow_answer=False)` для вызовов, которые
    ОБЯЗАНЫ получить цель (станционный `aifa-metric`)."""
    decision = await parse_message(
        raw_query, language=language, model_version=model_version,
        complete_fn=complete_fn, allow_answer=False,
    )
    if decision.goal is None:  # pragma: no cover — allow_answer=False: route="answer" невалиден
        raise IntentParseError("intent.parse: модель не вернула цель")
    return decision.goal


# ---------------------------------------------------------------------------
# A1.3/R4: «После валидации GoalIR чистая deterministic-функция строит один
# root Target» (`TargetFactory`, `26-arhitekturnaya-fiksaciya-v9.md`, §2).
# Отдельная функция, не метод `parse_intent` — `intent.parse` не обязан знать
# имена графовых артефактов (`"EvidenceBundleV2"`/`"FormOutput"` — детали
# `AtomRegistry`, Срез 3), это знание принадлежит слою, который уже граничит
# с планировщиком. `root_from_goal` — эта функция: имя сохранено с более
# ранней редакции (на него ссылаются существующие тесты), роль — ровно та,
# что документация v9 называет `TargetFactory`.
_FULL_ANALYSIS_OUTPUT = "ReportOutput"
_FORM_OUTPUT = "FormOutput"


def _metric_output_type(intent_kind: str) -> str:
    """R3.3/R4.4: extract vs calculate — разные `Target.output_type`, не один
    и тот же `Target(concept=X)`, разрешаемый через `PlanPolicy` tie-break
    между `read.value`/`calc.formula_engine` (см. `RETROFIT-REPORT-v9.md`,
    находка R3.3)."""
    return "CalculatedMetric" if intent_kind in _CALCULATE_KINDS else "ExtractedMetric"


class RootTarget(NamedTuple):
    """Один корень для `GraphPlanner.plan_ex(root, ..., root_extra=root_extra)`.

    `root_extra` — не поле `Target` (у него его нет), а третий позиционный
    параметр `plan_ex` (Срез 3, `graph_planner.py`): именно так роль сегодня
    доезжает до `out.form`/`analyze.full_bundle` (`dynamic_collection_scope`
    читает `node_args.extra.get("role")`). Пара `(root, root_extra)` держит их
    вместе, а не по отдельности, чтобы вызывающий код не мог передать один
    `Target` и чужой `root_extra`.

    `ad_hoc_spec` — R4.5: `AdHocConceptSpec`/`AdHocFormulaSpec`, когда корень
    построен из `extract_ad_hoc_metric`/`calculate_ad_hoc_metric` (иначе
    `None`). Живёт только здесь и в вызывающем коде текущего запроса — не
    записывается в `AtomRegistry`/canonical registry автоматически; `Case`
    (Срез 10), где он в итоге должен храниться на весь запрос, в этом
    репозитории ещё не существует."""

    root: Target
    root_extra: dict[str, str]
    ad_hoc_spec: AdHocConceptSpec | AdHocFormulaSpec | None = None
    goal_ref: str = "goal-1"
    goal_type: str = ""
    depends_on: tuple[str, ...] = ()


def root_from_goal(goal: GoalIR | GoalClause) -> list[RootTarget]:
    """`TargetFactory` (R4): один или несколько корней для `GraphPlanner.
    plan()` — по одному вызову `plan()`/`GraphExecutor.run()` на каждый
    элемент результата (см. докстринг `GoalIR`: `extract_registered_metric`/
    `calculate_registered_metric` могут назвать больше одного показателя,
    `intent-090` золотого набора). `full_analysis`/`form` — всегда ровно один
    синтетический корень, независимо от (пустого) `goal.targets`.

    Для видов из `_NOT_YET_WIRED_KINDS` (у которых пока нет producer'а в
    графе — их атомы приходят в более поздних срезах) поднимается явный
    `NotImplementedError`, а не выдумывается несуществующий графовый
    артефакт: `intent.parse` уже умеет их различать, но раскрыть в
    `PlanGraph` пока нечем.
    """
    if goal.intent_kind in _METRIC_KINDS_WITH_TARGETS:
        output_type = _metric_output_type(goal.intent_kind)
        roots = [
            RootTarget(
                root=Target(output_type=output_type, concept_id=t.concept,
                            entity_ref=goal.entity_ref, period_ref=goal.period_ref),
                root_extra={},
            )
            for t in goal.targets
        ]
        return [r._replace(goal_ref=goal.goal_ref, goal_type=goal.intent_kind,
                           depends_on=tuple(goal.depends_on)) for r in roots]
    if goal.intent_kind in _METRIC_KINDS_AD_HOC:
        # R4.5: однозначная незарегистрированная extraction/calculation
        # goal — временный AdHocConceptSpec/AdHocFormulaSpec из формулировки
        # пользователя (`unresolved[0]`, куда `_finalize_goal` кладёт
        # ДОСЛОВНУЮ фразу — см. `_SYSTEM_PROMPT`), не из выдуманного кода.
        # Сама формулировка — `Target.concept_id`: у неё нет записи в
        # `AtomRegistry`, но catch-all `OutputSpec(concept=None)`
        # (`read.value`/`calc.formula_engine`, `graph_atoms.py`) обслуживает
        # её наравне с зарегистрированными enum'ами.
        term = goal.unresolved[0] if goal.unresolved else goal.raw_query
        if goal.intent_kind == "calculate_ad_hoc_metric":
            # Срез 31 (П4): формулу и состав выдала МОДЕЛЬ (`_finalize_goal`
            # уже проверил выражение и компоненты детерминированно) — мост в
            # NodeArgs.extra теми же ключами, что читает `calc_formula.py::
            # _ad_hoc_spec_from_extra`, плюс описания компонентов для
            # двухстадийного экстрактора. Без доверенной формулы корень строить
            # незачем — он вернул бы пустой CalculatedMetric.
            raw_formula = goal.parameters.get("ad_hoc_formula")
            if not isinstance(raw_formula, dict):
                raise ValueError(
                    "calculate_ad_hoc_metric: модель не составила проверяемую формулу "
                    f"(expression и описание каждого компонента) для {term!r}"
                )
            spec = AdHocFormulaSpec.model_validate(raw_formula)
            term = spec.term
            root_extra = {
                "ad_hoc_spec_id": spec.spec_id,
                "ad_hoc_formula_expression": spec.expression,
                "ad_hoc_formula_dependencies": ",".join(spec.dependencies),
                "ad_hoc_formula_term": spec.term,
                "ad_hoc_formula_assumptions": spec.assumptions,
                "ad_hoc_formula_unit": spec.unit,
                AD_HOC_CONCEPTS_KEY: dump_specs(spec.components),
            }
        else:
            # Срез 31 (П5): понятие описано моделью (имя/синонимы/определение) —
            # экстрактор получит ту же форму строки, что у записи реестра.
            raw_concept = goal.parameters.get("ad_hoc_concept")
            spec = (
                AdHocConceptSpec.model_validate(raw_concept)
                if isinstance(raw_concept, dict) else AdHocConceptSpec(term=term)
            )
            term = spec.concept_key
            root_extra = {"ad_hoc_spec_id": spec.spec_id, AD_HOC_CONCEPTS_KEY: dump_specs([spec])}
        root = Target(output_type=_metric_output_type(goal.intent_kind), concept_id=term,
                       entity_ref=goal.entity_ref, period_ref=goal.period_ref)
        return [RootTarget(root=root, root_extra=root_extra, ad_hoc_spec=spec,
                           goal_ref=goal.goal_ref, goal_type=goal.intent_kind,
                           depends_on=tuple(goal.depends_on))]
    if goal.intent_kind == "full_analysis":
        output_type = _FULL_ANALYSIS_OUTPUT
    elif goal.intent_kind == "form":
        file_kind = str(goal.parameters.get("file_kind") or "").strip()
        if file_kind:
            if file_kind not in {"xlsx", "pdf", "csv", "json"}:
                raise ValueError(f"unsupported file_kind={file_kind!r}")
            return [RootTarget(
                root=Target(output_type="FileArtifact", entity_ref=goal.entity_ref, period_ref=goal.period_ref),
                root_extra={
                    "role": goal.role or "",
                    "source_output_type": _FORM_OUTPUT,
                    "file_kind": file_kind,
                    "template_version": str(goal.parameters.get("template_version") or "1"),
                }, goal_ref=goal.goal_ref, goal_type=goal.intent_kind,
                depends_on=tuple(goal.depends_on),
            )]
        output_type = _FORM_OUTPUT
    elif goal.intent_kind == "monitor_changes":
        # Срез 32: изменения между периодами ОДНОГО отчёта (не история снимков разных
        # документов). Период не назван → `all_available`: корень без периода, чтобы
        # `read.value` сохранил сравнительные колонки для `ref.prior`. Объяснение — по
        # данным документа, без веб-поиска (`external_context=off`); сегменты и состав
        # группы не извлекаются (`structures=off`) — таблица строится по показателям.
        return [RootTarget(
            root=Target(output_type="PeriodChangeReport", entity_ref=goal.entity_ref),
            root_extra={"role": goal.role or "", "external_context": "off", "structures": "off"},
            goal_ref=goal.goal_ref, goal_type=goal.intent_kind, depends_on=tuple(goal.depends_on),
        )]
    elif goal.intent_kind == "explain_change":
        concepts = [
            str(concept).strip().upper()
            for concept in goal.parameters.get("analysis_concepts") or []
            if str(concept).strip()
        ]
        if not concepts:
            return []
        # A change explanation is a composed analytical result, not N scalar
        # roots.  ``requested_concepts`` narrows both EvidenceBundleV2 and the
        # judgment/explanation collection to the concepts selected by
        # intent.parse.  The target deliberately has no period for
        # ``all_available``: read.value must retain the comparative columns
        # so ref.prior and the narrative can see the same evidence axis.
        all_available = not goal.period_ref
        return [RootTarget(
            root=Target(
                output_type="NarrativeOutput", entity_ref=goal.entity_ref,
                period_ref=None if all_available else goal.period_ref,
            ),
            root_extra={
                "requested_concepts": ",".join(concepts),
                "analysis_type": "explain_change",
                "depth": "detailed",
            }, goal_ref=goal.goal_ref, goal_type=goal.intent_kind,
            depends_on=tuple(goal.depends_on),
        )]
    elif goal.intent_kind in _NOT_YET_WIRED_KINDS:
        raise NotImplementedError(
            f"root_from_goal: goal_type={goal.intent_kind!r} распознаётся intent.parse (каталог 13 "
            "GoalType, docs/build-plan/26-arhitekturnaya-fiksaciya-v9.md), но producer для этого "
            "корня графа ещё не реализован в этом срезе."
        )
    else:  # pragma: no cover — intent_kind закрыт `Literal`, pydantic не пропустит иное
        raise ValueError(f"root_from_goal: неизвестный intent_kind {goal.intent_kind!r}")
    root = Target(output_type=output_type, entity_ref=goal.entity_ref, period_ref=goal.period_ref)
    root_extra = {"role": goal.role} if goal.role else {}
    return [RootTarget(root=root, root_extra=root_extra, goal_ref=goal.goal_ref,
                       goal_type=goal.intent_kind, depends_on=tuple(goal.depends_on))]


__all__ = [
    "ATOM_VERSION",
    "IngressContext",
    "IntentDecision",
    "IntentParseError",
    "candidate_concepts",
    "parse_intent",
    "parse_message",
    "root_from_goal",
    "RootTarget",
]
