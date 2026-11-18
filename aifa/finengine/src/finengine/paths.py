"""Где лежат не-питоновские файлы службы: один поиск на весь пакет.

ЗАЧЕМ ЭТОТ МОДУЛЬ (15.11.2026). Данные службы — `.md`-промпты, `.yaml`/`.json`-
справочники, `.xlsx`-спека календаря, сертификаты, whitelist инструментов — лежат
не в одном месте. Часть внутри пакета (`finengine/policy/POLICY.md`), часть рядом
с ним (`deploy/certs/`, `station-skills/`). Код искал их каждый по-своему:
`Path(__file__).with_name(...)` рядом с модулем, `parents[3] / "deploy"` от корня
репозитория, литерал `/app/src/finengine`.

В репозитории все три соглашения сходятся. В контейнере — нет: пакет приезжает в
`site-packages` (или внутрь PyInstaller-бандла, распакованного в `/tmp/_MEIxxxx`),
и `parents[3]` перестаёт указывать на `/app`. Отказ при этом тихий: сервис
поднимается, а функция ищет файл, которого на этом месте нет.

ПРАВИЛО ОДНО: корень данных определяется в одном месте, и каждая точка чтения
спрашивает его здесь. Порядок поиска:

  1. окружение — явное значение сильнее любого угадывания (`FINENGINE_DATA_ROOT`);
  2. `/app` — раскладка контейнера: `<корень>/deploy`, `<корень>/station-skills`,
     `<корень>/demo`, `<корень>/skills`;
  3. корень репозитория — четыре уровня выше пакета (`aifa/finengine`): локальная
     разработка и тесты видят ровно ту же раскладку, что и контейнер;
  4. последний родитель с ожидаемым подкаталогом — на случай нестандартной
     установки; сюда же попадает развёрнутый `dist/` каталог.

Каждый поиск возвращает `None`, если каталога нет, — вызывающий решает, отказ
это или штатное «работаем без этого». Молча подставлять первый попавшийся путь
нельзя: именно так `parents[3]` и приводил к чтению не того каталога.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DATA_ROOT_ENV",
    "PACKAGE_DIR",
    "data_root",
    "project_root",
    "repo_root",
    "find_dir",
    "find_package_dir",
    "find_package_file",
    "find_file",
]

#: Явный корень данных. Один и тот же смысл для контейнера и для локального запуска.
DATA_ROOT_ENV = "FINENGINE_DATA_ROOT"

#: Каталог установленного пакета. Может быть внутри PyInstaller-бандла — тогда
#: это временный `/tmp/_MEIxxxx`, и опираться на его родителей нельзя.
PACKAGE_DIR = Path(__file__).resolve().parent

#: Каталоги, по одному присутствию которых узнаётся корень данных.
_MARKERS = ("station-skills", "deploy", "skills")


def project_root() -> Path | None:
    """Корень проекта `aifa/finengine` — тот, где лежат `src/`, `deploy/`, `station-skills/`.

    Четыре уровня выше пакета — это раскладка репозитория:
    `finengine/skills/spec/paths.py` → `spec` → `skills` → `finengine` → `src` → корень.
    В контейнере и в бандле такой раскладки нет, и функция вернёт `None`.
    """
    for candidate in PACKAGE_DIR.parents:
        if (candidate / "src" / "finengine").is_dir() and (candidate / "deploy").is_dir():
            return candidate
    return None


def repo_root() -> Path | None:
    """Корень, от которого считаются `station-skills/`, `skills/`, `demo/`.

    В репозитории это каталог проекта (`aifa/finengine`), в контейнере — `/app`.
    Отличается от `project_root()` тем, что не требует `src/finengine`: служба
    в контейнере живёт без исходников.
    """
    explicit = os.environ.get(DATA_ROOT_ENV, "").strip()
    if explicit:
        return Path(explicit)
    app = Path("/app")
    if app.is_dir() and any((app / marker).is_dir() for marker in _MARKERS):
        return app
    project = project_root()
    if project is not None:
        return project
    for candidate in (PACKAGE_DIR, *PACKAGE_DIR.parents):
        if any((candidate / marker).is_dir() for marker in _MARKERS):
            return candidate
    return None


def data_root() -> Path | None:
    """Корень данных: то же, что `repo_root()`, но под явным именем для читателя."""
    return repo_root()


def find_dir(*parts: str) -> Path | None:
    """Каталог `<корень>/<parts...>`, если он есть; иначе `None`.

    `find_dir("station-skills", "aifa-analyze", "method")`
    """
    root = repo_root()
    if root is None:
        return None
    candidate = root.joinpath(*parts)
    return candidate if candidate.is_dir() else None


def find_file(*parts: str) -> Path | None:
    """Файл `<корень>/<parts...>`, если он есть; иначе `None`."""
    root = repo_root()
    if root is None:
        return None
    candidate = root.joinpath(*parts)
    return candidate if candidate.is_file() else None


def _external_copies(relative: str) -> list[Path]:
    """Внешние копии файла/каталога пакета — в порядке приоритета.

    Известные раскладки:
    - `<корень проекта>/src/finengine/<relative>` — репозиторий и тесты;
    - `/app/finengine-data/<relative>` — контейнер (Dockerfile кладёт сюда копию
      пакета, потому что бандл распаковывается в `/tmp/_MEIxxxx` и удаляется);
    - `/app/src/finengine/<relative>` — контейнер, где исходники всё-таки лежат;
    - `<копия проекта>/src/finengine/<relative>`, если копия проекта лежит
      прямо в каталоге пакета — раскладка PyInstaller-бандла обёртки AEF:
      её `--add-data /app:.` укладывает контекст сборки целиком, и проект
      `aifa/finengine` приезжает внутрь бандла как `finengine/` со всей
      репозиторной раскладкой `src/finengine/...`.

    Внешняя копия СТАРШЕ встроенной намеренно: методику правят без пересборки
    образа, и правка обязана побеждать то, что вморожено при сборке. Копия
    внутри бандла — наоборот, вморожена, поэтому стоит последней.
    """
    candidates: list[Path] = []

    project = project_root()
    if project is not None:
        candidates.append(project / "src" / "finengine" / relative)

    app = Path("/app")
    candidates.append(app / "finengine-data" / relative)
    candidates.append(app / "src" / "finengine" / relative)

    root = repo_root()
    if root is not None:
        candidates.append(root / "finengine-data" / relative)

    if (PACKAGE_DIR / "src" / "finengine").is_dir():
        candidates.append(PACKAGE_DIR / "src" / "finengine" / relative)

    return candidates


def _missing(kind: str, relative: str, tried: list[Path]) -> FileNotFoundError:
    where = ", ".join(str(path) for path in tried) or "<нечего пробовать>"
    return FileNotFoundError(f"нет {kind} данных {relative!r}: искали {where}")


def find_package_dir(relative: str) -> Path:
    """Каталог ВНУТРИ пакета: внешние копии по приоритету, затем встроенный.

    Возвращается всегда существующий каталог; если не нашлось ни одной копии, это
    ошибка раскладки, а не штатный режим «работаем без методики», — падаем сразу.
    """
    tried = _external_copies(relative)
    for candidate in tried:
        if candidate.is_dir():
            return candidate
    internal = PACKAGE_DIR / relative
    if internal.is_dir():
        return internal
    raise _missing("каталога", relative, [*tried, internal])


def find_package_file(relative: str) -> Path:
    """Файл внутри пакета: внешние копии по приоритету, затем встроенный."""
    tried = _external_copies(relative)
    for candidate in tried:
        if candidate.is_file():
            return candidate
    internal = PACKAGE_DIR / relative
    if internal.is_file():
        return internal
    raise _missing("файла", relative, [*tried, internal])
