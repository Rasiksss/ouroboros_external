"""Disk usage helpers for session rootfs accounting."""

from __future__ import annotations

import os
from pathlib import Path


def calculate_total_bytes(rootfs_path: Path) -> int:
    total = 0
    for current_root, _, filenames in os.walk(rootfs_path, followlinks=False):
        current = Path(current_root)
        for filename in filenames:
            target = current / filename
            if target.is_symlink() or not target.is_file():
                continue
            total += target.stat().st_size
    return total
