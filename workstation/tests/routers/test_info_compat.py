from __future__ import annotations

import asyncio

from app.routers import common


def test_info_returns_only_non_secret_deployment_metadata(monkeypatch) -> None:
    values = {
        "AGENT_ID": "agent-aifa",
        "POD_NAMESPACE": "pod-ns",
        "NAMESPACE": "fallback-ns",
        "SERVICE_NAME": "workstation",
        "APP_VERSION": "2026.8",
        "APP_LOG_LEVEL": "INFO",
        "DISTRIBUTIVE": "corp",
        "CLUSTER_ID": "cluster-a",
        "NODE_NAME": "node-a",
        "POD_NAME": "pod-a",
    }
    for name, value in values.items():
        monkeypatch.setattr(common.settings, name, value, raising=False)
    monkeypatch.setenv("INSTANCE_IP", "10.0.0.10")
    monkeypatch.setenv("HOST_IP", "10.0.0.11")

    payload = asyncio.run(common.info())

    assert payload == {
        "app": {
            "agentid": "agent-aifa",
            "namespace": "pod-ns",
            "name": "workstation",
            "version": "2026.8",
            "loglevel": "INFO",
            "distributive": "corp",
        },
        "pod": {
            "cluster": "cluster-a",
            "node": "node-a",
            "namespace": "fallback-ns",
            "pod_name": "pod-a",
            "instance_ip": "10.0.0.10",
            "host_ip": "10.0.0.11",
        },
    }

    serialized = repr(payload).lower()
    for forbidden in ("secret", "password", "token", "access_key"):
        assert forbidden not in serialized
