"""Ошибка провайдера модели — человеческим языком, в одном месте.

ОТКУДА ПЕРЕЕХАЛО (21.08.2026). Функция жила внутри
`skills/aifa_baseline/extractor.py` — движка, снятого этим же днём как легаси.
Её при этом звал БОЕВОЙ путь (`bulk_extractor/streams_full_context.py`), то есть
снос файла унёс бы с собой единственное место, где «402 insufficient credits»
превращается в «пополните баланс».

ПОЧЕМУ В `llm/`, А НЕ В `skills/`. Предмет — ответ ПРОВАЙДЕРА, а не наш домен;
звать её вправе любой, кто ходит к модели, и слой skills для этого слишком узок.
"""
from __future__ import annotations


def _user_facing_llm_error(raw: str) -> str:
    """Translate provider-side LLM errors into a short, user-actionable
    message. Shown as the error event when fail-fast trips."""
    low = (raw or "").lower()
    if "402" in low or "insufficient credits" in low:
        return (
            "Извлечение прервано: на аккаунте OpenRouter закончились кредиты. "
            "Пополните баланс и попробуйте снова."
        )
    if "401" in low or "unauthorized" in low or "invalid api key" in low:
        return "Извлечение прервано: ключ OpenRouter недействителен."
    if "429" in low or "rate limit" in low:
        return (
            "Извлечение прервано: провайдер ограничил скорость запросов. "
            "Подождите минуту и попробуйте снова."
        )
    if "timeout" in low or "timed out" in low:
        return "Извлечение прервано: модель не отвечает (таймаут)."
    return f"Извлечение прервано: модель вернула ошибку. {raw[:200]}"
