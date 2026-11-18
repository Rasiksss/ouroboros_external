"""Deployment invariants and migration startup behavior; no external services."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_mu_build_entries_and_runtime_commands_match():
    common = yaml.safe_load((ROOT / "deploy/mu/COMMON.example.yaml").read_text(encoding="utf-8"))
    agents = yaml.safe_load((ROOT / "deploy/mu/agents.ift.example.yaml").read_text(encoding="utf-8"))
    builds = {a["name"]: a for a in common["agents"]}
    assert set(builds) == {a["AGENT_NAME"] for a in agents}
    tiers = []
    for a in agents:
        context = ROOT.parent / builds[a["AGENT_NAME"]]["path"].lstrip("/")
        assert (context / "Dockerfile").is_file()
        assert all(isinstance(value, str) for value in a["ENV"].values())
        if "AGENT_CUSTOM_ARGS" not in a:
            continue
        module = a["AGENT_CUSTOM_ARGS"][-1]
        target = context / module.replace(".", "/")
        assert target.with_suffix(".py").is_file() or (target / "__main__.py").is_file()
        assert "OUROBOROS_MIGRATE_ON_START" not in a["ENV"]
        assert a["ENV"]["OUROBOROS_BROKER_BACKEND"] != "inprocess"
        assert a["ENV"]["OUROBOROS_SETTINGS_CACHE_BACKEND"] == "none"
        assert a["ENV"]["OUROBOROS_REPO_DIR"] == "/tmp/repo"
        tiers.append(a)
    assert len(tiers) == 5
    for key in [
        "OUROBOROS_KAFKA_TOPIC_PREFIX",
        "OUROBOROS_S3_BUCKET",
        "OUROBOROS_STORAGE_TENANT_ID",
    ]:
        assert len({a["ENV"][key] for a in tiers}) == 1
    aifa = next(a for a in agents if a["AGENT_NAME"] == "aifa")
    assert "TRANSPORT_SERVICE" in aifa["ENV"]["FINENGINE_OUROBOROS_URL"]
    assert aifa["RESOURCES"]["REPLICAS"] == 1
    assert aifa["ENV"]["FINENGINE_WORKERS"] == "1"
    assert aifa["ENV"]["FINENGINE_REDIS_URL"] == ""
    integrations = yaml.safe_load((ROOT / "deploy/mu/integrations.ift.example.yaml").read_text(encoding="utf-8"))
    assert not any(entry["NAME"] == "radish" for entry in integrations["POSTGRES"])
    for name in ["aifa-secrets.example", "gigaagent-secrets.example"]:
        assert "REDIS_URL=" not in (ROOT / "deploy/mu" / name).read_text(encoding="utf-8")


def _posix(path):
    path = path.resolve()
    return "/" + path.drive[0].lower() + path.as_posix()[2:] if os.name == "nt" else str(path)


@pytest.mark.parametrize("mode", [None, "0", "1", "true", "invalid"])
def test_entrypoint_never_runs_database_migrations(tmp_path, mode):
    bash = shutil.which("sh")
    if not bash:
        pytest.skip("sh required")
    env = os.environ.copy()
    env["OUROBOROS_REPO_DIR"] = "/app"
    if mode is not None:
        env["OUROBOROS_MIGRATE_ON_START"] = mode
    result = subprocess.run(
        [bash, str(ROOT / "entrypoint.sh"), "sh", "-c", "echo app-started"],
        env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "app-started"
