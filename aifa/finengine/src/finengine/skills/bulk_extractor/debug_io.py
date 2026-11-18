"""Сохранение сырых ответов модели на диск для разбора прогона.

Общий для стадий сегментов и состава группы: обе кладут ответ модели рядом с
прогоном, когда включён каталог отладки.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _write_llm_debug_file(debug_dir: Optional[Path], name: str, content: object) -> None:
    if debug_dir is None:
        return
    try:
        import json as _json

        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / name
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(_json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
