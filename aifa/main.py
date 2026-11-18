"""Точка входа AIFA для корпоративной обёртки AEF FastAPI.

Обёртка (`home.get_main_app`) собирает приложение PyInstaller-ом и ждёт в этом
модуле переменную `app` типа FastAPI. Раньше здесь стоял `uvicorn.run(...)`,
и обёртка падала на старте: `Exception: Должна быть переменная app типа FastAPI`.
Сервер поднимает сама обёртка, поэтому здесь только экспорт приложения.

🔴 ПОЧЕМУ ЗДЕСЬ ЕСТЬ ПОДСТРАХОВКА (15.11.2026). Обёртка wrapper
1.0.0.aef.fastapi собирает `app` САМА, своим PyInstaller-ом, и наших аргументов
(`--additional-hooks-dir`) не передаёт. Хук `hook-finengine.py` поэтому кладётся
ещё и в `<_pyinstaller>/hooks` внутри venv — это место PyInstaller читает всегда.
Но проверять это на каждом деплое дорого, а цена промаха — `ModuleNotFoundError:
No module named 'finengine.api'` и мёртвый под.

Поэтому данные (`/app/finengine-data`) работают ВСЕГДА: каталог копируется в
Dockerfile и здесь дописывается в конец `finengine.__path__`. Модули берутся из
бандла, найденные снаружи данные — оттуда. А если хук не сработал и подпакетов
`finengine.*` в бандле нет вовсе, добавляется ещё и путь к исходникам
`/app/src` — те же модули, которые иначе не импортировались бы совсем.
"""
import os
import sys
from pathlib import Path

_DATA_DIR_ENV = "FINENGINE_DATA_PACKAGE_DIR"
_DEFAULT_DATA_DIR = Path("/app/finengine-data")

_SOURCE_ROOT_ENV = "FINENGINE_SOURCE_ROOT"
_DEFAULT_SOURCE_ROOT = Path("/app/finengine-source")


def _mount_package_dir(env: str, default: Path) -> Path | None:
    """Дописать внешний каталог в конец `finengine.__path__`; None, если его нет."""
    directory = Path(os.environ.get(env) or default)
    if not directory.is_dir():
        return None
    try:
        from finengine import __path__ as package_path
    except ImportError:  # pragma: no cover - пакета нет, импорт ниже скажет об этом сам
        return None
    if str(directory) not in package_path:
        package_path.append(str(directory))
    return directory


def _mount_source_fallback() -> Path | None:
    """Исходники `/app/src` — только если подпакеты из бандла не импортируются.

    Сигнал отказа ровно тот, что виден в логах: `No module named 'finengine.api'`.
    Проверяем импортом, а не наличием каталога: если бандл собран правильно,
    лишний путь в `sys.path` не добавляется.
    """
    directory = Path(os.environ.get(_SOURCE_ROOT_ENV) or _DEFAULT_SOURCE_ROOT)
    if not (directory / "finengine" / "api" / "main.py").is_file():
        return None
    try:
        import finengine.api.main  # noqa: F401
    except ImportError:
        pass
    else:
        return None
    if str(directory) not in sys.path:
        sys.path.append(str(directory))
    return directory


DATA_DIR = _mount_package_dir(_DATA_DIR_ENV, _DEFAULT_DATA_DIR)
SOURCE_DIR = _mount_source_fallback()

from finengine.api.main import app  # noqa: E402,F401


def _serve() -> None:
    """Поднять сервер, если модуль запущен напрямую (`python /app/main.py`).

    Обёртка импортирует модуль и берёт из него `app` — тогда этот код не
    выполняется. Он нужен `entrypoint.sh` для запасного пути: когда бандл собрать
    не удалось, сервис поднимается интерпретатором, а не оставляет мёртвый под.
    """
    import uvicorn

    host = os.environ.get("FINENGINE_HOST", "0.0.0.0")
    port = int(os.environ.get("FINENGINE_PORT", "8080"))
    print(f"[main] запуск uvicorn на {host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    _serve()
