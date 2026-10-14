from __future__ import annotations

import pathlib

import pytest


def test_server_load_distribution_extensions_uses_repo_workspace(monkeypatch, tmp_path):
    import server

    calls = []
    provider = object()
    skill_bridge = object()
    skill_surface = object()

    def fake_reload_all(
        drive_root,
        settings_reader,
        *,
        skill_surface=None,
        skill_bridge=None,
        storage_provider=None,
    ):
        calls.append(
            {
                "drive_root": pathlib.Path(drive_root),
                "settings_reader": settings_reader,
                "skill_bridge": skill_bridge,
                "skill_surface": skill_surface,
                "storage_provider": storage_provider,
            }
        )
        return {"sample_ext": None}

    monkeypatch.setattr(server, "reload_all", fake_reload_all)
    monkeypatch.setattr(server, "get_storage_provider", lambda: provider, raising=False)

    result = server._load_distribution_extensions(tmp_path, skill_bridge, skill_surface)

    assert result == {"sample_ext": None}
    assert calls == [
        {
            "drive_root": tmp_path,
            "settings_reader": server.load_settings,
            # Handed down rather than fetched from a global: extensions reach the
            # transports through the bridge the composition root built.
            "skill_bridge": skill_bridge,
            "skill_surface": skill_surface,
            "storage_provider": provider,
        }
    ]


def test_server_load_distribution_extensions_fails_startup_on_distribution_error(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(
        server,
        "reload_all",
        lambda *_args, **_kwargs: {"tasks-scheduler": "registration failed"},
    )

    with pytest.raises(RuntimeError, match="tasks-scheduler.*registration failed"):
        server._load_distribution_extensions(tmp_path, object(), object())
