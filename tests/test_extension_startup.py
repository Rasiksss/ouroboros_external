from __future__ import annotations

import pathlib

import pytest


def test_server_load_enabled_extensions_uses_repo_workspace(monkeypatch, tmp_path):
    import server

    calls = []
    provider = object()

    def fake_reload_all(
        drive_root,
        settings_reader,
        *,
        workspace_root=None,
        repo_path=None,
        storage_provider=None,
    ):
        calls.append(
            {
                "drive_root": pathlib.Path(drive_root),
                "settings_reader": settings_reader,
                "workspace_root": pathlib.Path(workspace_root),
                "repo_path": repo_path,
                "storage_provider": storage_provider,
            }
        )
        return {"sample_ext": None}

    monkeypatch.setattr(server, "reload_all", fake_reload_all)
    monkeypatch.setattr(server, "get_storage_provider", lambda: provider, raising=False)

    result = server._load_enabled_extensions(tmp_path)

    assert result == {"sample_ext": None}
    assert calls == [
        {
            "drive_root": tmp_path,
            "settings_reader": server.load_settings,
            "workspace_root": pathlib.Path(server.REPO_DIR),
            "repo_path": None,
            "storage_provider": provider,
        }
    ]


def test_server_load_enabled_extensions_fails_startup_on_distribution_error(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(
        server,
        "reload_all",
        lambda *_args, **_kwargs: {"tasks-scheduler": "registration failed"},
    )

    with pytest.raises(RuntimeError, match="tasks-scheduler.*registration failed"):
        server._load_enabled_extensions(tmp_path)
