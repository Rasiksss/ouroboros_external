"""Tests for network bind helpers in policy/auth."""

import ouroboros.policy.auth as server_auth


def test_is_loopback_host_recognizes_common_local_hosts() -> None:
    assert server_auth.is_loopback_host("127.0.0.1")
    assert server_auth.is_loopback_host("localhost")
    assert server_auth.is_loopback_host("[::1]")
    assert not server_auth.is_loopback_host("0.0.0.0")
    assert not server_auth.is_loopback_host("192.168.1.10")
