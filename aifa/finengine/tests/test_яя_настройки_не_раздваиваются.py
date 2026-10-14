"""Объект настроек к концу прогона — тот же, что был на старте.

🔴 ЗАЧЕМ. importlib.reload(finengine.config) в ЛЮБОМ тесте исполняет config.py
повторно и перепривязывает `settings` к новому объекту; модули, взявшие
`from finengine.config import settings` на импорте, до конца прогона держат
осиротевший старый — правка настройки видна одному коду и не видна другому.
Цена, которой это куплено: сторож контуров ушёл в схему public и увидел 185
боевых документов стенда вместо своего одного (26–27.08.2026). Виновника
(reload в test_the_tables_do_not_travel_twice) нашло следствие по стеку
исполнений config.py; этот файл охраняет отсутствие рецидива.

⚠️ Имя файла НАМЕРЕННО кириллическое: «я» сортируется после всей латиницы, и
pytest собирает файл ПОСЛЕДНИМ — только так проверка видит весь прогон.
Одиночный запуск файла всегда зелен и ничего не доказывает.
"""
from __future__ import annotations


def test_settings_is_still_one_object():
    import finengine.auth.db as a
    import finengine.config as c

    assert c.settings is a.settings, (
        "объект настроек раздвоился: какой-то тест сделал importlib.reload"
        "(finengine.config) — найди его по traceback в config.py и замени "
        "reload на monkeypatch.setattr(settings, ...)")
