"""Разбор JSON из ответа модели, терпимый к обёрткам и мусору вокруг.

Модель то оборачивает ответ в ```json, то дописывает пояснение рядом. Этот
разбор общий для всех стадий — извлечения, разрезов, состава группы, — поэтому
стоит РАНО в порядке зависимостей, а не позже, как я сначала запланировал:
стадия разрезов упёрлась в него ссылкой вперёд, и ruff это поймал (F821).
"""
from __future__ import annotations



def _parse_extract_json(s: str) -> dict | None:
    """Robust JSON parser for full-context extract responses.

    Tries: (1) raw, (2) markdown-fence-stripped, (3) largest balanced {…} block.
    Returns None if no dict can be recovered. Used because models sometimes
    wrap output in ```json … ``` despite response_format=json_object hints.
    """
    import json as _json
    import re as _re
    s = (s or "").strip()
    if not s:
        return None
    s2 = _re.sub(r"^```(?:json)?\s*", "", s)
    s2 = _re.sub(r"\s*```$", "", s2)
    for cand in (s, s2):
        try:
            obj = _json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # Largest balanced {…}
    depth = 0
    start = -1
    best = None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blk = s[start:i + 1]
                if best is None or len(blk) > len(best):
                    best = blk
    if best:
        try:
            obj = _json.loads(best)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None
