"""The extension startup contract shared by the server and dedicated runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.skills.extensions import loader

if TYPE_CHECKING:
    from new_core.skills.surface import SkillSurface
    from ouroboros.storage.blob.protocol import StorageProvider
    from ouroboros.storage.settings.schema import SettingsLike
    from transport.skill_bridge import SkillBridge

log = logging.getLogger(__name__)


def unload_distribution_extensions() -> None:
    """Release registrations while their process-owned clients are still alive."""
    for name in loader.snapshot()["extensions"]:
        try:
            loader.unload_extension(name)
        except Exception:
            log.exception("Could not unload distribution extension %s", name)


def load_distribution_extensions(
    drive_root: Path,
    settings_reader: Callable[[], SettingsLike],
    *,
    skill_surface: SkillSurface,
    storage_provider: StorageProvider | None = None,
    skill_bridge: SkillBridge | None = None,
) -> dict[str, Any]:
    """Load the trusted surface before serving work; roll back partial startup."""
    try:
        results = loader.reload_all(
            drive_root,
            settings_reader,
            skill_surface=skill_surface,
            storage_provider=storage_provider,
            skill_bridge=skill_bridge,
        )
        failures = {name: error for name, error in results.items() if error and error != "disabled"}
        if failures:
            details = "; ".join(f"{name}: {error}" for name, error in sorted(failures.items()))
            raise RuntimeError(f"Distribution extension startup failed: {details}")
    except BaseException:
        unload_distribution_extensions()
        raise
    log.info("Distribution extension skills loaded count=%d", len(results))
    return results
