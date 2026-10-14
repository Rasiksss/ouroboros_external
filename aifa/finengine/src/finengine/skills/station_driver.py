"""ПОЛКА станции: сборка `.shelf.zip` — пакет finengine + тела навыков.

⚠️ ЧЕМ ЭТОТ ФАЙЛ БЫЛ ДО #166. Он поднимал комнату станции сам: создавал
сессию, заливал `ocr.txt` и полку, звал `run.sh` навыка и релеил события
обратно. Это и был единственный исполнитель извлечения и разбора, и звали его
ровно из двух мест — обоих внутри снесённых монолитов.

Теперь комнату поднимает МОДЕЛЬ через `exec`, полку в неё кладёт плагин
гейтвея (`deploy/workstation-ext/index.ts`), а события приезжают на нонс хода
(`api/station_turn.py`). От файла осталась сборка полки — и остался он потому,
что `skills/versioning.py` снимает по ней отпечаток версий прогона.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

_shelf_cache: bytes | None = None


def _shelf_roots() -> list[tuple[str, Path]]:
    app = Path("/app/src/finengine")
    if app.exists():
        return [("finengine", app), ("aifa_station_skills", Path("/app/station-skills"))]
    base = Path(__file__).resolve().parents[2]  # …/src
    return [("finengine", base / "finengine"),
            ("aifa_station_skills", base.parent / "station-skills")]


def _build_shelf() -> bytes:
    """Zip the backend's OWN finengine + station-skills → the station runs the
    EXACT engine version the backend has (strict parity, no baked drift). Cached."""
    global _shelf_cache
    if _shelf_cache is not None:
        return _shelf_cache
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, root in _shelf_roots():
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                    z.write(p, f"{arc}/{p.relative_to(root)}")
    _shelf_cache = buf.getvalue()
    return _shelf_cache


def shelf_read(arcname: str) -> bytes | None:
    """Файл ИЗ УЖЕ СОБРАННОЙ полки — той самой, что уехала в станцию.

    ЗАЧЕМ (#155). Полка собирается ОДИН РАЗ за жизнь процесса (`_shelf_cache`) и
    дальше исполняется как есть, а файлы на диске бэкенда читаются заново на
    каждый вызов. На правке реестра «без деплоя» — сценарий, которым мотивирован
    #153, — эти два источника расходятся: станция считает по СТАРЫМ правилам, а
    паспорт, читающий диск, уверенно называет НОВЫЙ отпечаток. Молча. Поэтому
    паспорт снимается отсюда: полка и есть признак версии того, что исполнялось.

    `None` — полки ещё нет (станция в этом процессе не запускалась). Собирать её
    здесь НЕЛЬЗЯ: это была бы полка, которую никто не исполнял, то есть ровно та
    подмена источника, от которой мы уходим. Вызывающий обязан сказать словами,
    что читал диск.
    """
    if _shelf_cache is None:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(_shelf_cache)) as z:
            return z.read(arcname)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None

