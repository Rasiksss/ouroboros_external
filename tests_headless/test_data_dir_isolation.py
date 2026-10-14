"""No test in this root resolves its data directory to the developer's own.

`tests/` has had this guard since a run wrote into `~/Ouroboros/data`; this root did not,
and then gained `-n 8`. DATA_DIR, SETTINGS_PATH and PORT_FILE are single files, so eight
worker processes sharing them share them with whatever runtime the machine actually has.
"""

from __future__ import annotations

import pathlib


def test_the_data_dir_is_this_test_s_own(tmp_path: pathlib.Path) -> None:
    from ouroboros.app import config

    resolved = pathlib.Path(config.DATA_DIR).resolve()

    assert resolved != (pathlib.Path.home() / "Ouroboros" / "data").resolve()
    assert tmp_path.resolve() in resolved.parents


def test_the_settings_and_port_files_follow_it(tmp_path: pathlib.Path) -> None:
    """Both are single files, which is what makes a shared root a parallel-run collision."""
    from ouroboros.app import config

    data_dir = pathlib.Path(config.DATA_DIR).resolve()

    assert data_dir in pathlib.Path(config.SETTINGS_PATH).resolve().parents
    assert data_dir in pathlib.Path(config.PORT_FILE).resolve().parents
