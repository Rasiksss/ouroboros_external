"""External metric definitions use the same Slice-13 layer compiler.

Slice 14 deliberately removes the former ``<profile>.json`` adapter. An
external deployment may still mount registry data without rebuilding the
image, but it must mount a versioned ``manifest.json`` plus layers. This keeps
one precedence/provenance/validation mechanism for built-in and external data.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from finengine.config import settings
from finengine.skills import metric_schema
from finengine.skills.metric_schema import registry_for_profile, registry_path_for_profile


BUILTIN = Path(metric_schema.__file__).resolve().parent.parent / "data" / "registry_layers"


def _external_layers(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "registry_layers"
    shutil.copytree(BUILTIN, target)
    monkeypatch.setattr(settings, "registry_dir", str(target))
    metric_schema._RUNTIME_BY_VERSION.clear()
    return target


def test_external_manifest_uses_the_canonical_layer_compiler(tmp_path, monkeypatch):
    target = _external_layers(tmp_path, monkeypatch)

    external = registry_for_profile("ifrs_bank_ru")
    monkeypatch.setattr(settings, "registry_dir", "")
    metric_schema._RUNTIME_BY_VERSION.clear()
    builtin = registry_for_profile("ifrs_bank_ru")

    assert external == builtin
    assert (target / "manifest.json").is_file()


def test_complete_profile_file_is_not_a_runtime_override(tmp_path, monkeypatch, caplog):
    target = tmp_path / "registry"
    target.mkdir()
    (target / "ifrs_bank_ru.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "registry_dir", str(target))
    metric_schema._RUNTIME_BY_VERSION.clear()

    with caplog.at_level("WARNING", logger=metric_schema.__name__):
        registry = registry_for_profile("ifrs_bank_ru")

    assert registry["extractable"]
    assert "не содержит manifest.json" in caplog.text
    assert registry_path_for_profile("ifrs_bank_ru") == BUILTIN / "manifest.json"


def test_missing_external_directory_is_loud_and_falls_back_to_layers(
    tmp_path, monkeypatch, caplog,
):
    missing = tmp_path / "missing"
    monkeypatch.setattr(settings, "registry_dir", str(missing))
    metric_schema._RUNTIME_BY_VERSION.clear()
    metric_schema._LAST_WARNED.clear()

    with caplog.at_level("WARNING", logger=metric_schema.__name__):
        registry = registry_for_profile("ifrs_nf_ru")

    assert registry["extractable"]
    assert str(missing) in caplog.text


def test_registry_source_path_is_always_a_manifest(tmp_path, monkeypatch):
    target = _external_layers(tmp_path, monkeypatch)
    assert registry_path_for_profile("ifrs_nf_ru") == target / "manifest.json"
