"""Хук PyInstaller для finengine: подпакеты и файлы данных.

ЗАЧЕМ ЭТОТ ХУК. PyInstaller собирает то, что видит статический анализ импортов.
`finengine` грузится подпакетами: `finengine.api`, `finengine.skills`, ... — и
многие из них импортируются динамически или через `__init__`, который анализатор
не проходит насквозь. Без списка `hiddenimports` в бандл попадает только часть
подпакетов, и сервис падает с `ModuleNotFoundError: No module named 'finengine.api'`.

Та же беда с данными: `.md`-промпты методик, `.yaml`/`.json`-справочники — не код,
PyInstaller их не забирает. А `finengine.policy` читает `POLICY.md` на ИМПОРТЕ,
то есть без файла падает весь сервис, а не одна функция.

ПОЧЕМУ ТУТ ДВА ПУТИ К ИМЕНИ ПАКЕТА. Сборку делает не только наш Dockerfile: обёртка
AEF (wrapper 1.0.0.aef.fastapi) собирает бандл из `main.py` сама, своим
PyInstaller-ом, и hooks передаёт только если сама на них укажет. Поэтому хуку нужно
уметь находить пакет и там, где он НЕ установлен в venv:

  1. штатный импорт — пакет установлен (наш случай, `pip install finengine/`);
  2. каталог рядом с `main.py` — `/app/src/finengine` или `/app/finengine-data`.

Второй путь подключается только когда первый пуст: `collect_*` берут список
каталогов поиска из `sys.path`, а добавлять туда каталог с исходниками — значит
рискнуть подмешать `.py` вместо установленных. `--paths` у нас нет намеренно.

Хук вызывается PyInstaller-ом ДО сборки и обязан молчать, когда пакета нет:
`hiddenimports = []` и `datas = []` — это «собирай как раньше», а не ошибка.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

_NAME = "finengine"
# Расширения, которые нужны службе в рантайме. Список закрытый: `*.py` в данные
# не попадает (это код), а `*.pem` возим отдельно — сертификаты лежат в deploy/.
_DATA_PATTERNS = ["**/*.md", "**/*.yaml", "**/*.yml", "**/*.json", "**/*.txt", "**/*.csv"]


def _is_importable() -> bool:
    """Установлен ли пакет в окружении сборки (наш venv)."""
    from importlib.util import find_spec

    try:
        return find_spec(_NAME) is not None
    except (ImportError, ValueError):
        return False


def _fallback_source_dirs() -> list[str]:
    """Каталоги, где может лежать НЕустановленный пакет, с учётом раскладки образа.

    Возвращаются только существующие каталоги: `/app/src` и `/app/finengine-source`
    содержат сам пакет `finengine` как подкаталог.
    """
    roots = (
        Path("/app/finengine-source"),  # копия пакета рядом с main.py (см. Dockerfile)
        Path("/app/src"),
        Path("/app"),
        Path.cwd(),
    )
    found: list[str] = []
    for root in roots:
        if (root / _NAME).is_dir() and str(root) not in found:
            found.append(str(root))
    return found


def _prepare_search_path() -> None:
    """Дописать каталог пакета в `sys.path`, если штатно его не видно."""
    if _is_importable():
        return
    for directory in _fallback_source_dirs():
        if directory not in sys.path:
            sys.path.insert(0, directory)
    # Путь к пакету мог быть найден — проверим ещё раз, уже с правкой sys.path.


_prepare_search_path()

if _is_importable():
    hiddenimports = collect_submodules(
        _NAME, filter=lambda name: not name.rsplit(".", 1)[-1].startswith("test")
    )
    datas = collect_data_files(_NAME, includes=_DATA_PATTERNS)
else:
    # Пакета не видно ни в venv, ни в известных раскладках: не мешаем сборке и
    # говорим об этом громко — молча собранный бандл потом падает на старте.
    print(
        f"[hook-{_NAME}] ПАКЕТ НЕ НАЙДЕН: ни установленный, ни в "
        f"{_fallback_source_dirs()}. Бандл соберётся без {_NAME}.",
        file=sys.stderr,
    )
    hiddenimports: list[str] = []
    datas: list[tuple[str, str]] = []

__all__ = ["datas", "hiddenimports"]
