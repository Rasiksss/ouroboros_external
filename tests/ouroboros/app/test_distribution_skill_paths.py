from __future__ import annotations

import subprocess
import sys


def test_legacy_data_skill_path_helper_is_removed() -> None:
    from ouroboros.app import config

    assert not hasattr(config, "ensure_data_skills_dir")


def test_installed_distribution_exposes_new_core_runtime_package(tmp_path) -> None:
    imported = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from new_core.loop import max_steps_for",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert imported.returncode == 0, imported.stderr
