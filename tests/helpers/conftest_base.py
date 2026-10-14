"""Shared conftest logic for tests/ and tests_headless/.

Both conftest files compose a runtime store, bootstrap config, and wrap
monkeypatch.setenv to auto-refresh the config singleton.  The logic is
identical; only the profile (development vs headless) differs.
"""

from __future__ import annotations

import pathlib

import pytest


def fail_reads_of(monkeypatch, filename: str, exc: Exception) -> None:
    """Make one file present-but-unreadable, by name."""
    original = pathlib.Path.read_text

    def _read_text(self, *args, **kwargs):
        if self.name == filename:
            raise exc
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", _read_text)


def refresh_test_config() -> None:
    """Rebuild config singleton from the current os.environ (call after monkeypatch)."""
    from ouroboros.app.config import _refresh_path_aliases
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    reset_config_for_tests()
    bootstrap_config(force=True)
    _refresh_path_aliases()
    compose_runtime_store_for_settings()


def wrap_monkeypatch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap monkeypatch.setenv/delenv to auto-refresh config on every env change."""
    real_setenv = monkeypatch.setenv
    real_delenv = monkeypatch.delenv

    def setenv(name: str, value: str, raising: bool = True) -> None:
        del raising
        real_setenv(name, value)
        refresh_test_config()

    def delenv(name: str, raising: bool = True) -> None:
        real_delenv(name, raising=raising)
        refresh_test_config()

    monkeypatch.setenv = setenv  # type: ignore[assignment]
    monkeypatch.delenv = delenv  # type: ignore[method-assign]


_composed_store_key: tuple[str, ...] | None = None
_REAL_APP_ROOT = pathlib.Path.home() / "Ouroboros"


def reject_real_data_dir(root: pathlib.Path) -> None:
    """Refuse to compose a test store on the developer's own runtime data."""
    if root == _REAL_APP_ROOT or _REAL_APP_ROOT in root.parents:
        raise RuntimeError(
            f"a test resolved its data dir to the real one ({root}); keep OUROBOROS_DATA_DIR pointed at tmp_path"
        )


def compose_runtime_store_for_settings() -> None:
    """Point the process's one runtime store at whatever the current settings resolve to.

    Keyed on the URLs and not on the drive root: the root no longer locates the database,
    so keying on it would make each of the hundreds of ``monkeypatch.setenv`` calls in the
    suite dispose a live engine and build another against the same schema.
    """
    global _composed_store_key
    from ouroboros.storage.db_url import canonicalize_runtime_db_urls, collect_runtime_db_urls
    from ouroboros.storage.file_transfers import initialize_transfer_limits
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.paths import load_path_settings
    from ouroboros.storage.settings.store import load_settings

    paths = load_path_settings()
    root = pathlib.Path(paths.drive_root or paths.data_dir)
    reject_real_data_dir(root)
    try:
        key = tuple(canonicalize_runtime_db_urls(collect_runtime_db_urls(load_settings())))
        if _composed_store_key != key:
            runtime_persistence.close()
        store = runtime_persistence.initialize()
        initialize_transfer_limits(store.engine, load_settings())
    except ValueError:
        # A configuration the runtime refuses is what a refusal test hands it on purpose,
        # including one that names no database at all. Raising here would fail the fixture
        # instead of the code under test; the next refresh composes again from scratch.
        _composed_store_key = None
        return
    _composed_store_key = key


def forget_composed_runtime_store() -> None:
    """Close the composed store between tests, so the next one composes its own."""
    global _composed_store_key
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.database import dispose_probe_database

    runtime_persistence.close()
    dispose_probe_database()
    _composed_store_key = None


def _actor_type_for_seed_entry(entry: dict) -> str:
    direction = str(entry.get("direction") or "in").lower()
    if direction in ("out", "outgoing"):
        return "agent"
    if direction == "system":
        return "system"
    return "owner"


def seed_chat_entries(drive_root: pathlib.Path, entries: list[dict]) -> list[str]:
    """Persist chat-like dict entries to the runtime store; return message ids."""
    from ouroboros.storage.chat_store import append_chat_message
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.scope import scope_for

    runtime_persistence.initialize()
    message_ids: list[str] = []
    for index, entry in enumerate(entries):
        message_id = str(entry.get("client_message_id") or f"seed-{index}")
        source = str(entry.get("source") or "dialogs")
        chat_id = entry.get("chat_id", 1)
        persisted = append_chat_message(
            drive_root,
            scope=scope_for(source, chat_id),
            actor_type=_actor_type_for_seed_entry(entry),  # type: ignore[arg-type]
            chat_id=chat_id,
            user_id=entry.get("user_id", 1),
            text=str(entry.get("text") or ""),
            source=source,
            client_message_id=message_id,
            ts=str(entry.get("ts") or "") or None,
            message_type=str(entry.get("type") or ""),
            extra_metadata={key: entry[key] for key in ("tool_calls", "rounds") if key in entry},
        )
        assert persisted is not None
        message_ids.append(persisted.watermark_id)
    return message_ids
