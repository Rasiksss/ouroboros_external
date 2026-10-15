"""Слова человека → имена реестра ячейки: один мост на все входы (Ш13).

ОТКУДА ПЕРЕЕХАЛО. Функция жила внутри тула `fe_extract_metrics`
(`llm/tools.py::_resolve_requested_metrics`). Тул уходит — комнату поднимает
гейтвей, а список показателей теперь собирает скрипт навыка в станции. Оставить
резолюцию в умирающем модуле значило бы, что вместе с ним умрёт и способность
понимать «выручка» вместо `SALES_REVENUE`.

ПОЧЕМУ ЗДЕСЬ, А НЕ В СКРИПТЕ НАВЫКА. Просить показатели словами вправе и модель
через навык, и любой будущий вход; реестр и поиск по нему живут в `skills/`.
Копия в станционном скрипте была бы вторым местом, где живёт одно правило.

ЧТО ЗДЕСЬ ЕЩЁ ЕСТЬ И ПОЧЕМУ. `check_shape` — проверка ФОРМЫ имени. Раньше её
делал снимаемый драйвер станции (`station_driver.py`,
`re.fullmatch(r"[A-Z0-9_]{1,64}")`) — там она страховала шелл-интерполяцию на
бэкенде. Интерполяция не исчезла: командную строку теперь собирает обёртка
`extract_run.sh`. Резолюция по реестру строго сильнее регулярки, но она НЕ
проверяет то, ради чего регулярка стояла, — что строка вообще пригодна как
аргумент командной строки.
"""
from __future__ import annotations

# 64 знака — потолок имени в реестре с запасом; 200 показателей — больше, чем
# есть в самой широкой ячейке, то есть просьба длиннее заведомо не про реестр.
_MAX_NAME = 64
_MAX_ITEMS = 200


class RequestShapeError(ValueError):
    """Просьба не годится как аргумент командной строки — говорим, а не режем."""


def check_shape(asked: list[str]) -> None:
    """Отказать ДО реестра, если имя не может быть именем.

    Управляющие символы и переводы строк отсекаются не ради красоты: строка
    уезжает в командную строку станции, и кавычка с переводом строки внутри
    имени — это чужая команда, а не показатель.
    """
    if len(asked) > _MAX_ITEMS:
        raise RequestShapeError(
            f"запрошено {len(asked)} показателей, потолок {_MAX_ITEMS} — "
            "это уже не список показателей")
    for word in asked:
        if not word or len(word) > _MAX_NAME:
            raise RequestShapeError(
                f"имя показателя {word[:80]!r} длиннее {_MAX_NAME} знаков или пустое")
        if any(ch.isspace() and ch != " " for ch in word) or any(ord(ch) < 32 for ch in word):
            raise RequestShapeError(
                f"в имени показателя {word[:80]!r} управляющие символы")


def resolve_requested(asked: list[str], profile_id: str) -> tuple[list[str], list[str]]:
    """Слова человека → имена реестра ячейки. Возвращает (найденные, нет).

    Просить показатели можно и enum'ами, и словами: модель видит наши имена в
    ответе `fe_registry_find`, но человек говорит «выручка». Готовое имя
    пропускаем как есть, остальное отдаём тому же детерминированному мосту.

    ОТБРОШЕННЫЕ ИМЕНА ВОЗВРАЩАЮТСЯ ОТДЕЛЬНО не для порядка: молча выкинуть
    непонятое — значит отдать таблицу из трёх показателей на просьбу о
    четырёх, и разницу заметит только тот, кто пересчитает строки.
    """
    if not asked:
        return [], []
    try:
        from finengine.skills.metric_schema import registry_for_profile

        reg = registry_for_profile(profile_id)
        known = {str(m["enum"]).strip().upper() for m in reg["extractable"]}
        known |= {str(c[0]).strip().upper() for c in reg["calculated"]}
    except FileNotFoundError:
        known = set()
    found: list[str] = []
    unresolved: list[str] = []
    for word in asked:
        if word.strip().upper() in known:
            found.append(word.strip().upper())
        else:
            unresolved.append(word)
    if unresolved:
        from finengine.skills.registry_find import find_metrics

        hits = find_metrics(unresolved, profile_id, limit=1)
        for word in list(unresolved):
            rows = (hits.get("matches") or {}).get(word) or []
            if rows:
                found.append(str(rows[0]["enum"]).strip().upper())
                unresolved.remove(word)
    # dict.fromkeys — порядок просьбы сохраняется, дубли уходят.
    return list(dict.fromkeys(found)), unresolved
