"""Сессии агента: история диалога с моделью. Владелец — часть ключа.

Своё имя у этого модуля есть, а своего хранилища больше нет: под ним общий
механизм `store/state.py` («вид, владелец, ключ → документ»). Имя оставлено
намеренно — граница владельца должна быть видна прямо в подписи вызова, а не
угадываться из строкового вида.

🔴 ПОЧЕМУ ЭТО НЕ REDIS, ХОТЯ ПРАВИЛО 3.2 НАЗЫВАЕТ REDIS ДЛЯ СЕССИЙ. Правило
говорит про состояние ХОДА — то, что живёт, пока держится соединение. Здесь
другое: история диалога, и её задача прямо обратная — пережить рестарт и
деплой (в прежнем коде так и было написано). У Redis вытеснение по памяти;
потерянная история — это потерянный разговор пользователя, а не подогретый
кэш. Оперативное состояние хода едет в Redis отдельно (`store/cache.py`).

🔴 ГРАНИЦА ВЛАДЕЛЬЦА МЕНЯЕТ ПРИРОДУ ПРИ ПЕРЕЕЗДЕ, И ЭТО САМОЕ ОПАСНОЕ МЕСТО.
Была ФИЗИЧЕСКАЯ: сессии лежали в `data/sessions/<owner_id>/`, и чужой
`session_id` в каталоге владельца просто не находился — так и было записано в
комментарии роута восстановления треда: «cross-user load is physically
impossible». В базе стены нет: все сессии в одной таблице, и отделяет их ровно
условие `owner_key` в запросе. Забыть его — значит открыть чтение чужих
переписок, и ни один тест на «сессия сохраняется и грузится» этого не заметит:
свой владелец читает своё одинаково успешно и с условием, и без него.

Поэтому свойство «чужую не отдаём» проверяется тестом на ОБЕИХ реализациях
сразу (`tests/test_sessions_keep_their_owner_boundary.py`). Требование
`docs/rules/security.md` 3.1 — «проверка владельца ресурса на каждом
защищённом роуте».

ПЕРЕКЛЮЧАТЕЛЬ — общий, `FINENGINE_STATE_DATABASE_URL`. Пусто = файлы, как было.
"""
from __future__ import annotations

from finengine.store import state


def owner_key(owner_id: int | None) -> str:
    """Ключ владельца. Собирается одной функцией — и для файлов, и для базы.

    `None` — это `_global`, а не «любой»: у ходов без авторизации свой
    namespace, и смешивать его с чужими нельзя.
    """
    return state.safe_segment(owner_id if owner_id is not None else "_global")


class SessionStore:
    """Тонкая обёртка: подпись с владельцем поверх общего состояния."""

    def __init__(self, store: state.StateStore):
        self._store = store

    def save(self, owner_id: int | None, session_id: str, data: dict) -> None:
        self._store.put(state.SESSION, owner_key(owner_id),
                        state.safe_segment(session_id), data)

    def load(self, owner_id: int | None, session_id: str) -> dict | None:
        return self._store.get(state.SESSION, owner_key(owner_id),
                               state.safe_segment(session_id))


def LocalSessionStore() -> SessionStore:  # noqa: N802 — фабрика с именем класса
    """Файлы на томе. Имя сохранено: на него ссылаются тесты и переезд."""
    return SessionStore(state.LocalStateStore())


def SqlSessionStore(url: str) -> SessionStore:  # noqa: N802 — то же
    """База."""
    return SessionStore(state.SqlStateStore(url))


def get_session_store() -> SessionStore:
    """ПЕРЕКЛЮЧАТЕЛЬ — общий с остальным состоянием."""
    return SessionStore(state.get_state_store())
