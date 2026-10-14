from __future__ import annotations

import importlib.util


def test_legacy_skill_migrations_module_is_retired():
    """The v5.8 native-to-external topology repair window is closed."""
    assert importlib.util.find_spec("ouroboros.skill_migrations") is None
