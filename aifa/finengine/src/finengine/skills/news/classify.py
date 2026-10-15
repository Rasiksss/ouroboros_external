"""Step 4: single LLM batch call to classify ~30 candidates.

This is the only place we touch an LLM in the digest pipeline. We send
a compact JSON array (id, source, region, published_at, title, snippet)
and expect a strict JSON array back with the same `id` and added
classification fields. No multi-turn, no tools.

Cost target: ~30 items × ~150 tokens prompt + ~30 items × 80 tokens
output ≈ 7000 in + 2400 out ≈ $0.005 per digest on DeepSeek V3.
"""
from __future__ import annotations

import json
import logging

from finengine.config import settings
from finengine.skills.news.models import (
    ClassifiedItem,
    NewsItem,
)

log = logging.getLogger(__name__)


_SYSTEM = (
    "Ты — фильтр новостей IR-аналитика Сбербанка. На вход — список новостных "
    "кандидатов в JSON. Твоя задача: КЛАССИФИЦИРОВАТЬ каждую новость и вернуть "
    "СТРОГО JSON-объект с одним ключом `results`, значение которого — массив "
    "ровно того же размера что и input, в том же порядке. По одному объекту на "
    "вход. Не добавляй preamble, не добавляй markdown — только JSON.\n\n"
    'Формат ответа: {"results": [{"id": 0, ...}, {"id": 1, ...}, ...]}\n\n'
    "Поля каждого объекта в results:\n"
    '  id              — копируй id из входа\n'
    '  is_relevant     — true если новость имеет финансово-рыночное значение, иначе false\n'
    '  category        — "macro" (макропоказатели), "regulator" (решения регуляторов), '
    '"corporate" (корп. события компаний), "unknown" (если не подходит)\n'
    '  region          — "RU" | "US" | "EU" | "CN" | "global"\n'
    '  importance      — float 0.0..1.0, влияние на рынок (НЕ на банк-владельца)\n'
    '  one_sentence_ru — 1 предложение по-русски, до 200 символов, с конкретикой '
    '(цифрами если есть). Без оценок, без прогнозов. Только факт.\n'
    '  why_important   — короткая (до 100 chars) причина почему importance именно такой\n'
    '  confidence      — float 0.0..1.0, уверенность в классификации\n\n'
    "ВАЖНО:\n"
    "  • не выдумывай факты которых нет в title/snippet — если непонятно, "
    "is_relevant=false и confidence низкий\n"
    "  • спорт, политика без рыночных последствий, lifestyle → is_relevant=false\n"
    "  • если событие про конкретную компанию из списка — category='corporate'\n"
    "  • если про макропоказатель (CPI, GDP, ставка, безработица, ВВП, инфляция, "
    "промпроизводство, розничные продажи, безработица, занятость, M2, ОФЗ) → 'macro'\n"
    "  • если официальное решение регулятора (ЦБ, ФРС, ЕЦБ, ФАС, SEC, Минфин РФ/США) "
    "→ 'regulator', is_relevant=true ВСЕГДА\n"
    "  • importance>=0.7 — для событий которые двинут индексы / ставки / котировки "
    "крупных компаний; <0.3 — для локальных/слабых сигналов\n"
    "  • category='regulator' или 'macro' от **official** источника (cbr.ru, federalreserve, "
    "ecb, bea, bls, sec, eia) — importance>=0.5 ВСЕГДА (рутинная макростатистика тоже "
    "имеет ценность для IR)\n"
    "  • source с display_name '(e-disclosure)' — это раскрытие существенного факта "
    "по компании из watchlist (matched_entities заполнен). is_relevant=true ВСЕГДА, "
    "category='corporate', importance>=0.5. Заголовок там типа «Завершение размещения "
    "ценных бумаг» или «Решения совета директоров» — переведи в one_sentence_ru с "
    "именем компании из matched_entities.\n\n"
    # Четыре правила куплены разбором живого выпуска 17.08: чужая новость под
    # компанией watchlist, два верных числа Brent без привязок = противоречие,
    # гарантия названа инвестицией, одно событие вышло двумя пунктами.
    "ЧЕСТНОСТЬ ВЫПУСКА — эти правила важнее краткости:\n"
    "  • КОМПАНИЯ. Ставь новость под компанию ТОЛЬКО если новость О ней или прямо "
    "меняет её положение. Упоминание в перечислении, в списке лидеров роста/падения "
    "или в общей сводке рынка — НЕ основание: такая новость идёт без компании "
    "(category='macro'). matched_entities — подсказка поиска, а не приговор.\n"
    "  • ЦЕНЫ. Сохраняй временную привязку источника: «внутридневной максимум», "
    "«на закрытие», «на 19:00 мск». Без неё два верных числа за один день "
    "превращаются в противоречие.\n"
    "  • МОДАЛЬНОСТЬ. «инвестирует», «предоставит гарантию», «выдаст кредит», "
    "«ведёт переговоры» — разные обязательства. Бери формулировку источника, "
    "не подменяй и не усиливай её.\n"
    # Порядок предпочтения в правиле ДУБЛЯ куплен третьим заходом: прежний
    # «агентство, официальный сайт» гасил копию с ленты ЦБ в пользу пересказа
    # Интерфакса, блок регуляторов оставался пустым при живом событии и
    # печатал «Свежих публикаций нет» — ложь выпуска о самом себе.
    "  • ДУБЛЬ. Ты видишь весь батч сразу. Если несколько входов об ОДНОМ событии "
    "(та же компания, та же сумма или факт, тот же день) — оставь ОДИН, "
    "предпочитая первичный источник пересказу в порядке: официальный сайт "
    "регулятора или эмитента → агентство → пересказ, а остальным is_relevant=false. "
    "Гасится ПЕРЕСКАЗ: пункт с ленты самого регулятора гасить нельзя, иначе блок "
    "регуляторов выйдет пустым при живом событии. Запрет на дубль сильнее правил "
    "«is_relevant=true ВСЕГДА» — иначе одно решение регулятора выйдет трижды."
)


def _build_candidate_json(items: list[NewsItem]) -> list[dict]:
    """Compact JSON for the LLM. Drop fields that don't help classification
    (URL, source_weight, hashes, etc.) — they only inflate context."""
    return [
        {
            "id": i,
            "source": it.source,
            "source_type": it.source_type,  # ← LLM uses this for macro/reg importance boost
            "region": it.region,
            "published_at": it.published_at.isoformat(),
            "title": it.title[:200],
            "snippet": it.snippet[:300],
            "matched_entities": it.matched_entities[:5],
            "matched_topics": it.matched_topics[:5],
        }
        for i, it in enumerate(items)
    ]


def _parse_response(raw: str, items: list[NewsItem]) -> list[ClassifiedItem]:
    """Be tolerant of common LLM JSON gotchas: leading/trailing markdown
    fences, extra prose, escaped newlines, single-quoted strings."""
    text = raw.strip()
    # Strip ```json ... ``` fence if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()

    # If model wrapped in {"items": [...]} dig in
    #
    # 🔴 `strict=False` — НЕ послабление, а признание факта (19.08.2026).
    # Живой прогон на проде: «Invalid control character at line 325 column 27».
    # Модель вернула СМЫСЛОВО ВЕРНЫЙ разбор шестидесяти новостей, но внутри
    # одной строки поставила буквальный перевод строки — по букве RFC это
    # недопустимо, и `json.loads` отверг ВЕСЬ ответ. Итог: выпуск с «0 компаний»
    # при 199 отобранных новостях, и прогон записан успешным. Ронять шестьдесят
    # разборов из-за одного \n в тексте пересказа — цена, которую платит
    # читатель, а не модель.
    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        log.error("classify: LLM returned non-JSON: %s\n--- raw ---\n%s", e, raw[:1000])
        raise ValueError(f"ответ модели не разобран: {e}") from e

    # LLMs love wrapping arrays in various keys when forced to return a
    # JSON object. Look for common shapes before giving up.
    if isinstance(parsed, dict):
        for k in ("results", "items", "classified", "data", "news"):
            if k in parsed and isinstance(parsed[k], list):
                parsed = parsed[k]
                break
        else:
            log.error(
                "classify: dict response with no array key (keys=%s); raw=%s",
                list(parsed.keys())[:5], raw[:500],
            )
            return []
    if not isinstance(parsed, list):
        log.error("classify: expected array, got %s; raw=%s",
                  type(parsed).__name__, raw[:500])
        return []

    out: list[ClassifiedItem] = []
    by_id: dict[int, dict] = {}
    for obj in parsed:
        if isinstance(obj, dict) and "id" in obj:
            try:
                by_id[int(obj["id"])] = obj
            except (TypeError, ValueError):
                continue

    for i, item in enumerate(items):
        obj = by_id.get(i)
        if obj is None:
            # LLM dropped this item — keep as not-relevant defaults
            out.append(ClassifiedItem(
                item=item, is_relevant=False, category="unknown",
                region=item.region, importance=0.0, one_sentence_ru="",
                why_important="missing in LLM response", confidence=0.0,
            ))
            continue
        out.append(ClassifiedItem(
            item=item,
            is_relevant=bool(obj.get("is_relevant", False)),
            category=str(obj.get("category", "unknown")).lower() if obj.get("category") in
                     ("macro", "regulator", "corporate", "unknown") else "unknown",  # type: ignore[assignment]
            region=str(obj.get("region", item.region)),  # type: ignore[assignment]
            importance=float(obj.get("importance", 0.0) or 0.0),
            one_sentence_ru=str(obj.get("one_sentence_ru", "") or "")[:300],
            why_important=str(obj.get("why_important", "") or "")[:200],
            confidence=float(obj.get("confidence", 0.5) or 0.5),
        ))
    return out


async def classify_batch(
    items: list[NewsItem],
    *,
    model: str | None = None,
    focus: str = "",
) -> tuple[list[ClassifiedItem], dict]:
    """One LLM round-trip for all items. Returns (classified, usage).
    Usage dict: {in, out, cached, model, cost_usd}."""
    if not items:
        return [], {"in": 0, "out": 0, "cached": 0, "model": "", "cost_usd": 0.0}

    from finengine.llm import openrouter as llm
    from finengine.llm.pricing import cost_usd

    user_msg = (
        "## Кандидаты для классификации\n\n"
        + json.dumps(_build_candidate_json(items), ensure_ascii=False, indent=2)
        + "\n\nВерни JSON-массив той же длины ("
        + str(len(items))
        + " элементов), порядок ровно тот же, поля как в инструкции."
    )
    # #90: фокус пользователя (инструкция сценария) СУЖАЕТ выборку — вне фокуса
    # is_relevant=false. В user_msg (не в _SYSTEM): системный промпт стабилен.
    if focus.strip():
        user_msg += (
            "\n\n## Фокус пользователя (обязательный фильтр)\n"
            + focus.strip()
            + "\nНовости, НЕ подходящие под этот фокус, помечай is_relevant=false. "
            "Фокус сужает выборку, а не расширяет."
        )

    # Модель — из настройки, а не литералом: см. `config.news_classify_model`.
    # Явно переданная аргументом всё ещё сильнее.
    chosen_model = model or settings.news_classify_model

    log.info("classify: sending %d candidates to %s", len(items), chosen_model)
    try:
        text, usage = await llm.complete_with_usage(
            prompt=user_msg,
            system=_SYSTEM,
            model=chosen_model,
            temperature=0.0,
            response_format={"type": "json_object"},
            # 🔴 БЮДЖЕТ СЧИТАЕТСЯ ОТ ЧИСЛА КАНДИДАТОВ (21.08.2026).
            # Раньше стояло 24000 литералом — под 60 кандидатов. Потолок
            # кандидатов стал настройкой (`news_top_candidates`), и литерал
            # разошёлся бы с ним молча: на 100 кандидатах ответ обрывается на
            # полуслове, а это ровно тот отказ, которым куплены предыдущие две
            # редакции этой строки.
            #
            # Токены размышлений идут в ЭТОТ ЖЕ счёт — на том и горели:
            # 17.08 бюджет 9000 дал 29 287 знаков размышлений и НИ ОДНОГО знака
            # ответа, выпуск вышел с нулями при 942 собранных новостях.
            #
            # Замер по 12 прогонам на 60 кандидатах: выход 10 965–19 904
            # токенов, то есть 183–332 на кандидата. Берём 350 на кандидата
            # плюс 8000 на размышления — на 60 выходит 29 000 (было 24 000,
            # запас честный), на 100 — 43 000. Модель принимает до 64 000
            # (проверено). Неиспользованный бюджет не оплачивается.
            max_output=350 * len(items) + 8000,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("classify: LLM call failed: %s", e)
        return [], {"in": 0, "out": 0, "cached": 0, "model": chosen_model, "cost_usd": 0.0,
                    "error": str(e)}

    classified = _parse_response(text, items)
    usage["cost_usd"] = cost_usd(usage)
    log.info("classify: got %d items back, cost=$%.4f (in=%d out=%d)",
             len(classified), usage["cost_usd"],
             usage.get("in", 0), usage.get("out", 0))
    return classified, usage
