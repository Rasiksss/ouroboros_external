"""The unit suite resolves its data dir under ``tmp_path``, never the real one."""

import pathlib

import pytest

from tests.conftest import _REAL_APP_ROOT, _reject_real_data_dir


@pytest.fixture
def data_dir_after_every_teardown():
    """Assert on the resolved data dir once every other fixture of the test has finalized.

    Finalizers run LIFO, so this fixture — requested first, therefore set up first — is torn
    down last. That is where a fixture that unsets ``OUROBOROS_DATA_DIR`` shows up.
    """
    from ouroboros.storage.settings.paths import load_path_settings

    yield
    resolved = pathlib.Path(load_path_settings().data_dir)
    assert _REAL_APP_ROOT not in resolved.parents, f"teardown left the data dir at {resolved}"


def test_composing_a_store_under_the_real_app_root_is_refused(tmp_path):
    _reject_real_data_dir(tmp_path / "data")

    with pytest.raises(RuntimeError, match="real one"):
        _reject_real_data_dir(_REAL_APP_ROOT / "data")


def test_isolate_settings_keeps_the_data_dir_isolated_through_teardown(
    data_dir_after_every_teardown, isolate_settings, tmp_path
):
    assert pathlib.Path(isolate_settings.DATA_DIR) == tmp_path / "data"
