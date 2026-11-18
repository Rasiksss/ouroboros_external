"""Единый хендлер финансового анализа (документо-ориентированный).

Один заземлённый проход модели по ТЕКСТУ отчёта (не по таблице метрик) →
структурированный markdown по каркасу ячейки. Глубина `quick|full` меняет окно
текста, состав разделов и объём вывода. Ячейка (company_type × standard ×
language) — параметр промпта, не отдельный скилл.
"""
from __future__ import annotations

from finengine.llm import openrouter as llm
from finengine.skills.analysis import framework
from finengine.skills.reasoner.runner import (
    build_metrics_table_str,
    pages_in_table,
    scrub_page_refs,
)


async def run(
    *,
    depth: str = "full",
    company_name: str,
    company_type: str,
    standard: str = "ifrs",
    ocr_text: str,
    metrics: list[dict] | None = None,
    model_id: str | None = None,
    progress=None,
    capital_facts: str = "",
    methodology=None,
    external_facts: list | None = None,
    lens: tuple[str, ...] = (),
) -> str:
    depth = "quick" if str(depth).lower() == "quick" else "full"
    if progress is not None:
        try:
            label = "быстрый анализ" if depth == "quick" else "полный анализ"
            progress.put_nowait(f"{label}: формирую отчёт…")
        except Exception:  # noqa: BLE001
            pass

    # Метрики — ОСНОВА, а не helper (#238, требование владельца 14.08). Здесь
    # значилось «ОПЦИОНАЛЬНЫЙ helper… анализ строится на тексте отчёта», и это
    # была не оговорка, а работающее поведение: без таблицы промпт терял блок
    # запрета арифметики (он подставлялся только при непустой таблице) и
    # оставлял одно разрешение — «все числа бери из текста отчёта». Разбор,
    # собранный так, от разбора по таблице в выдаче НЕ отличим.
    #
    # Рассчитываемые (#74: посчитаны Python-формулами, не LLM) помечаем в имени,
    # чтобы модель видела, каким значениям верить без пересчёта.
    if not metrics:
        raise ValueError(
            "анализ без таблицы показателей не собирается: числа в выводах берутся "
            "из прогона извлечения и посчитанных по нему коэффициентов")
    metrics = [
        {**m, "name_ru": f"{m.get('name_ru') or m.get('enum') or ''} [расчёт кодом]"}
        if m.get("is_calculated") else m
        for m in metrics
    ]
    metrics_table = build_metrics_table_str(metrics)
    # Что реально доехало в таблицу заземления (#151). Определения
    # коэффициентов в методике заявлены как «посчитаны кодом в таблице»; у
    # индийской банковской ячейки Cost/Income, NIM и стоимости риска в реестре
    # нет — по такому отчёту фраза была прямой неправдой, и модель получала
    # определение величины, которой в таблице не будет, плюс запрет считать
    # самой. Берём ФАКТ прогона, а не список ячеек: заведут формулы —
    # определения вернутся сами.
    available_enums = {
        str(m.get("enum") or "").strip().upper()
        for m in (metrics or []) if m.get("found") is not False
    } - {""}

    # capital_facts (регуляторные нормативы) извлекаются фокусным мини-вызовом в
    # вызывающем коде (tools._h_fe_analyze) и подаются сюда как факт для промпта
    # (заземление; впечатывание в структуру карточки снято — #73).
    # Язык изложения НЕ параметр вызова: он один на продукт и живёт в
    # framework.answer_language(). Иначе каждый путь (инлайн-тул, станционный
    # скилл) решал бы сам — так и появился английский разбор Nestlé.
    # methodology — эффективная методика (база + личные правки пользователя, #123).
    # Не передали — базовая, та, что поставляется с продуктом.
    # external_facts (новости/макро/регулятор) — тем же приёмом, что capital_facts:
    # собраны КОДОМ на бэкенде до анализа и поданы фактом (#159). Здесь их только
    # прокидывают: этот вызов идёт БЕЗ инструментов, в сеть отсюда не сходить.
    prompt = framework.build_prompt(
        company_name=company_name, company_type=company_type, standard=standard,
        depth=depth, ocr_text=ocr_text,
        metrics_table=metrics_table, capital_facts=capital_facts,
        methodology=methodology,
        available_enums=available_enums or None,
        external_facts=external_facts,
        # Призмы (канон §3/§8) прокидываются НАСКВОЗЬ: решение «какая призма
        # нужна» принимает модель по телу навыка, здесь его не пересматривают.
        lens=tuple(lens or ()),
    )
    try:
        out, _ = await llm.complete_with_usage(
            prompt=prompt, system=None, model=model_id,
            max_output=framework.MAX_OUTPUT.get(depth, 2000),
        )
    except Exception as e:  # noqa: BLE001
        return f"_(анализ не сгенерирован: {e})_"

    body = (out or "").strip()
    if not body:
        return "_(анализ не сгенерирован: пустой ответ модели)_"
    # Ссылки на страницы — только те, что движок ПОКАЗАЛ в таблице заземления
    # (#157). Промпт просит брать номер из таблицы, но проверяем мы данными, а
    # не доверием: сверяем со страницами, вычитанными обратно из той же строки,
    # что ушла модели. Чужой номер теряет формат ссылки и получает честную
    # пометку — фронт делает кликабельным ровно «PDF стр. N», и выдуманная
    # страница физически не может стать ссылкой.
    body = scrub_page_refs(body, pages_in_table(metrics_table))
    title = "Финансовый анализ" + ("" if depth == "full" else " (быстрый)")
    return f"# {title} — {company_name or 'компания'}\n\n{body}"
