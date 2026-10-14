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
    monkeypatch.setattr(server, "_install_termination_handlers", lambda: None)
    monkeypatch.setattr(server, "_DrainingServer", FakeServer)
    server._stop_requested.clear()
    server._restart_on_exit.clear()

    assert server.main() == 0
    assert cleanup_calls == []


def test_main_graceful_restart_cleanup_avoids_port_sweep(monkeypatch):
    import server

    cleanup_calls = []

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            server._request_restart_exit()
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
    monkeypatch.setattr(server, "_install_termination_handlers", lambda: None)
    monkeypatch.setattr(server, "_DrainingServer", FakeServer)
    server._stop_requested.clear()
    server._restart_on_exit.clear()

    exit_codes = []
    try:
        server.main()
    except ExitCalled as exc:
        exit_codes.append(exc.args[0])
    finally:
        server._stop_requested.clear()
        server._restart_on_exit.clear()

    assert cleanup_calls == [{"port_sweep": False}]
    # Direct headless restart: re-exec is attempted, and the exit-code fallback
    # stays 42 for anything supervising the process.
    assert len(restart_calls) == 1
    assert exit_codes == [server.RESTART_EXIT_CODE]


def test_a_termination_signal_asks_for_a_shutdown_the_process_does_not_come_back_from():
    """SIGTERM means stop; re-execing after it would fight whatever asked for the stop."""
    import server

    server._stop_requested.clear()
    server._restart_on_exit.clear()
    try:
        server._request_termination(15)

        assert server._stop_requested.is_set() is True
        assert server._restart_on_exit.is_set() is False
    finally:
        server._stop_requested.clear()


def test_an_in_band_restart_request_still_asks_for_the_process_back():
    import server

    server._stop_requested.clear()
    server._restart_on_exit.clear()
    try:
        server._request_restart_exit()

        assert server._stop_requested.is_set() is True
        assert server._restart_on_exit.is_set() is True
    finally:
        server._stop_requested.clear()
        server._restart_on_exit.clear()


def test_a_signal_seen_during_the_run_starts_the_drain_rather_than_exiting(monkeypatch):
    """uvicorn owns the handler while it serves, so the drain has to start through its hook."""
    import server

    server._stop_requested.clear()
    server._restart_on_exit.clear()
    handled = []
    monkeypatch.setattr(server.uvicorn.Server, "handle_exit", lambda self, sig, frame: handled.append(sig))
    draining = server._DrainingServer.__new__(server._DrainingServer)
    try:
        draining.handle_exit(15, None)

        assert handled == [15]
        assert server._stop_requested.is_set() is True
        assert server._restart_on_exit.is_set() is False
    finally:
        server._stop_requested.clear()


def test_the_force_exit_watchdog_outlives_the_drain_it_guards(monkeypatch):
    """A watchdog under the drain kills the hand-back it exists to allow.

    The 30 seconds predate the drain. With `OUROBOROS_DRAIN_TIMEOUT_SEC` alone taking 20 of
    them, and the A2A join another five, firing mid-drain leaves the held runs `running`
    under a lease nobody renews — they close later as `lease_lost` rather than
    `instance_released`.
    """
    import server

    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(OUROBOROS_DRAIN_TIMEOUT_SEC=20))

    assert server._drain_timeout_sec() == 20
