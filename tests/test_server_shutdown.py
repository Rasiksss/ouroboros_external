from types import SimpleNamespace


def test_main_normal_exit_does_not_run_emergency_cleanup(monkeypatch):
    import server

    cleanup_calls = []

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(OUROBOROS_SERVER_HOST="127.0.0.1"))
    monkeypatch.setattr(server, "parse_server_args", lambda *_a, **_k: SimpleNamespace(host="127.0.0.1", port=8765))
    monkeypatch.setattr(server, "find_free_port", lambda _host, port: port)
    monkeypatch.setattr(server, "write_port_file", lambda *_a, **_k: None)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: object())
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_emergency_process_cleanup", lambda: cleanup_calls.append("cleanup"))
    server._restart_requested.clear()

    assert server.main() == 0
    assert cleanup_calls == []


def test_main_graceful_restart_cleanup_avoids_port_sweep(monkeypatch):
    import server

    cleanup_calls = []

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            server._restart_requested.set()
            return None

    class ExitCalled(RuntimeError):
        pass

    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(OUROBOROS_SERVER_HOST="127.0.0.1"))
    monkeypatch.setattr(server, "parse_server_args", lambda *_a, **_k: SimpleNamespace(host="127.0.0.1", port=8765))
    monkeypatch.setattr(server, "find_free_port", lambda _host, port: port)
    monkeypatch.setattr(server, "write_port_file", lambda *_a, **_k: None)
    restart_calls = []
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: object())
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_restart_current_process", lambda *a, **_k: restart_calls.append(a))
    monkeypatch.setattr(server, "_emergency_process_cleanup", lambda **kw: cleanup_calls.append(kw))
    monkeypatch.setattr(server.os, "_exit", lambda code: (_ for _ in ()).throw(ExitCalled(code)))
    server._restart_requested.clear()

    exit_codes = []
    try:
        server.main()
    except ExitCalled as exc:
        exit_codes.append(exc.args[0])
    finally:
        server._restart_requested.clear()

    assert cleanup_calls == [{"port_sweep": False}]
    # Direct headless restart: re-exec is attempted, and the exit-code fallback
    # stays 42 for anything supervising the process.
    assert len(restart_calls) == 1
    assert exit_codes == [server.RESTART_EXIT_CODE]
