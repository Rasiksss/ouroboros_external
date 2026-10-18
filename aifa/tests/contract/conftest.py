"""Контрактные проверки используют модули записывающего клиента (`golden_*`) как есть."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
