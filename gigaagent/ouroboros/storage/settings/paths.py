"""Filesystem paths resolved from bootstrap configuration sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOME = Path.home()

RESTART_EXIT_CODE = 42
AGENT_SERVER_PORT = 8080


@dataclass(frozen=True)
class PathSettings:
    """Resolved Ouroboros directory layout (bootstrap-only keys, not in AppSettings)."""

    app_root: Path
    repo_dir: Path
    data_dir: Path
    settings_path: Path
    pid_file: Path
    port_file: Path
    server_port: int = AGENT_SERVER_PORT
    agent_python: str = ""
    drive_root: Path | None = None


def resolve_path_settings(sources: Mapping[str, Any]) -> PathSettings:
    app_root = Path(sources.get("OUROBOROS_APP_ROOT") or HOME / "Ouroboros")
    data_dir = Path(sources.get("OUROBOROS_DATA_DIR") or app_root / "data")
    raw_port = str(sources.get("OUROBOROS_SERVER_PORT") or AGENT_SERVER_PORT).strip()
    try:
        server_port = int(raw_port)
    except (TypeError, ValueError):
        server_port = AGENT_SERVER_PORT
    drive_raw = str(sources.get("OUROBOROS_DRIVE_ROOT") or "").strip()
    return PathSettings(
        app_root=app_root,
        repo_dir=Path(sources.get("OUROBOROS_REPO_DIR") or app_root / "repo"),
        data_dir=data_dir,
        settings_path=Path(sources.get("OUROBOROS_SETTINGS_PATH") or data_dir / "settings.json"),
        pid_file=Path(sources.get("OUROBOROS_PID_FILE") or app_root / "ouroboros.pid"),
        port_file=Path(sources.get("OUROBOROS_PORT_FILE") or data_dir / "state" / "server_port"),
        server_port=server_port,
        agent_python=str(sources.get("OUROBOROS_AGENT_PYTHON") or "").strip(),
        drive_root=Path(drive_raw) if drive_raw else None,
    )


def load_path_settings() -> PathSettings:
    from .bootstrap import get_config

    return get_config().paths


# Module-level aliases matching legacy config.py exports (refreshed on config reload).
def _refresh_path_module_aliases() -> None:
    global APP_ROOT, REPO_DIR, DATA_DIR, SETTINGS_PATH, PID_FILE, PORT_FILE
    paths = load_path_settings()
    APP_ROOT = paths.app_root
    REPO_DIR = paths.repo_dir
    DATA_DIR = paths.data_dir
    SETTINGS_PATH = paths.settings_path
    PID_FILE = paths.pid_file
    PORT_FILE = paths.port_file


APP_ROOT = HOME / "Ouroboros"
REPO_DIR = APP_ROOT / "repo"
DATA_DIR = APP_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
PID_FILE = APP_ROOT / "ouroboros.pid"
PORT_FILE = DATA_DIR / "state" / "server_port"
