from __future__ import annotations

import sys
from types import SimpleNamespace


def test_server_subcommand_sanitizes_argv(monkeypatch):
    from ouroboros.app import cli

    seen = {}

    class FakeServer:
        @staticmethod
        def main():
            seen["argv"] = list(sys.argv)
            return 0

    monkeypatch.setitem(sys.modules, "server", FakeServer)
    monkeypatch.setattr(sys, "argv", ["ouroboros", "server", "--host", "127.0.0.1", "--port", "9000"])
    monkeypatch.delenv("OUROBOROS_ENV", raising=False)

    result = cli._server_command(SimpleNamespace(host="127.0.0.1", port=9000, no_ui=True))

    assert result == 0
    assert seen["argv"] == ["ouroboros"]
    assert sys.argv == ["ouroboros", "server", "--host", "127.0.0.1", "--port", "9000"]
    assert cli.os.environ["OUROBOROS_ENV"] == "production"
