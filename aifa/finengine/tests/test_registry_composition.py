"""Срез 13: канонический реестр собирается слоями, не копиями профилей.

Golden создан ДО нового compiler из десяти прежних self-contained JSON после
того же runtime-слияния aliases, которое видел extraction. Поэтому parity здесь
не сравнивает новый код с самим собой: каждое поле и порядок строк сверяются с
зафиксированным старым поведением.
"""
from __future__ import annotations

import ast
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from finengine.contracts import EntityIdentity, RunPassport
from finengine.skills.atoms.run_passport import classification_from_cell
from finengine.skills import station_driver
from finengine.skills.metric_schema import compiled_registry_for_classification
from finengine.skills.registry_composition import (
    BUILTIN_LAYER_DIR,
    LayerConflictError,
    RegistryCompositionError,
    RegistrySelection,
    compile_registry,
    freeze_registry,
    registry_for_passport,
)


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "registry_profiles_v1.json"
PROFILE_IDS = (
    "ifrs_nf_ru", "ifrs_bank_ru", "rsbu_nf_ru", "rsbu_bank_ru",
    "us_gaap_nf_en", "us_gaap_bank_en", "ifrs_nf_en", "ifrs_bank_en",
    "ind_as_nf_en", "ind_gaap_bank_en",
)


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["profiles"]


def _selection(profile_id: str, *, customer_id: str = "") -> RegistrySelection:
    c = classification_from_cell(profile_id, customer_id=customer_id)
    return RegistrySelection.from_classification(c)


def _copy_layers(tmp_path: Path) -> Path:
    target = tmp_path / "registry_layers"
    shutil.copytree(BUILTIN_LAYER_DIR, target)
    return target


def _add_layer(root: Path, payload: dict) -> None:
    filename = f"{payload['layer_id'].replace('.', '__')}.json"
    (root / filename).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["layers"].append(filename)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _customer_layer(
    layer_id: str,
    customer_id: str,
    *,
    alias: str,
    formula: str = "TOTAL_ASSETS / TOTAL_EQUITY",
    priority: int = 400,
) -> dict:
    old_aliases = next(
        row["aliases_en"]
        for row in _golden()["ifrs_nf_en"]["extractable"]
        if row["enum"] == "TOTAL_ASSETS"
    )
    return {
        "schema_version": "registry-layer/v1",
        "layer_id": layer_id,
        "version": "customer-2026.09.22.1",
        "kind": "customer",
        "priority": priority,
        "selector": {"customer_id": customer_id},
        "metrics": {
            "extractable": [{
                "enum": "TOTAL_ASSETS",
                "fields": {"aliases_en": [*old_aliases, alias]},
            }],
            "calculated": [{
                "enum": "CUSTOMER_ASSET_EQUITY",
                "fields": {
                    "name_en": "Customer asset/equity",
                    "formula": formula,
                    "deps": ["TOTAL_ASSETS", "TOTAL_EQUITY"],
                    "min_value": 0.0,
                    "max_value": 100.0,
                    "role": "customer",
                },
            }],
        },
    }


def test_manifest_versions_the_exact_precedence_model() -> None:
    manifest = json.loads((BUILTIN_LAYER_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["precedence_version"] == "standard-company-industry-language-customer/v1"
    assert manifest["priorities"] == {
        "standard": 100,
        "company_type": 200,
        "industry": 210,
        "language": 300,
        "customer": 400,
    }


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_all_ten_profiles_have_exact_field_by_field_parity(profile_id: str) -> None:
    compiled = compile_registry(_selection(profile_id))
    assert compiled.to_effective_data() == _golden()[profile_id]


def test_compiler_and_hash_are_stable_under_manifest_and_json_key_order(tmp_path: Path) -> None:
    before = compile_registry(_selection("ifrs_bank_ru"))
    copied = _copy_layers(tmp_path)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["layers"].reverse()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for path in copied.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    after = compile_registry(_selection("ifrs_bank_ru"), layer_dir=copied)
    assert after.to_effective_data() == before.to_effective_data()
    assert after.fingerprint == before.fingerprint


def test_customer_override_carries_field_provenance(tmp_path: Path) -> None:
    copied = _copy_layers(tmp_path)
    _add_layer(copied, _customer_layer(
        "customer.acme", "acme", alias="ACME assets",
    ))
    compiled = compile_registry(_selection("ifrs_nf_en", customer_id="acme"), layer_dir=copied)
    metric = compiled.metric("extractable", "TOTAL_ASSETS")
    assert metric.fields["aliases_en"][-1] == "ACME assets"
    source = metric.provenance["aliases_en"]
    assert (source.layer_id, source.version, source.priority) == (
        "customer.acme", "customer-2026.09.22.1", 400,
    )


def test_two_different_values_at_the_same_priority_are_compile_error(tmp_path: Path) -> None:
    copied = _copy_layers(tmp_path)
    _add_layer(copied, _customer_layer("customer.acme.a", "acme", alias="A"))
    _add_layer(copied, _customer_layer("customer.acme.b", "acme", alias="B"))
    with pytest.raises(LayerConflictError, match="same priority"):
        compile_registry(_selection("ifrs_nf_en", customer_id="acme"), layer_dir=copied)


def test_customer_layer_is_isolated_and_adds_alias_formula_and_thresholds_as_data(
    tmp_path: Path,
) -> None:
    copied = _copy_layers(tmp_path)
    baseline = compile_registry(_selection("ifrs_nf_en"), layer_dir=copied)
    _add_layer(copied, _customer_layer(
        "customer.acme", "acme", alias="ACME assets", formula="TOTAL_ASSETS / TOTAL_EQUITY",
    ))
    acme = compile_registry(_selection("ifrs_nf_en", customer_id="acme"), layer_dir=copied)
    other = compile_registry(_selection("ifrs_nf_en", customer_id="other"), layer_dir=copied)

    assert other.to_effective_data() == baseline.to_effective_data()
    assert acme.fingerprint != baseline.fingerprint
    assert acme.metric("extractable", "TOTAL_ASSETS").fields["aliases_en"][-1] == "ACME assets"
    formula = acme.metric("calculated", "CUSTOMER_ASSET_EQUITY").fields
    assert formula["formula"] == "TOTAL_ASSETS / TOTAL_EQUITY"
    assert (formula["min_value"], formula["max_value"]) == (0.0, 100.0)


def test_industry_layer_is_selected_by_the_id_frame_axis(tmp_path: Path) -> None:
    copied = _copy_layers(tmp_path)
    base = compile_registry(_selection("ifrs_nf_en"), layer_dir=copied)
    original = base.metric("extractable", "TOTAL_ASSETS").fields["description"]
    _add_layer(copied, {
        "schema_version": "registry-layer/v1",
        "layer_id": "industry.energy",
        "version": "industry-2026.09.22.1",
        "kind": "industry",
        "priority": 210,
        "selector": {"standard": "ifrs", "industry": "energy"},
        "metrics": {"extractable": [{
            "enum": "TOTAL_ASSETS",
            "fields": {"description": "Energy-industry total assets"},
        }]},
    })
    energy = compile_registry(
        RegistrySelection.from_profile_id("ifrs_nf_en", industry="energy"),
        layer_dir=copied,
    )
    retail = compile_registry(
        RegistrySelection.from_profile_id("ifrs_nf_en", industry="retail"),
        layer_dir=copied,
    )

    metric = energy.metric("extractable", "TOTAL_ASSETS")
    assert metric.fields["description"] == "Energy-industry total assets"
    assert metric.provenance["description"].layer_id == "industry.energy"
    assert retail.metric("extractable", "TOTAL_ASSETS").fields["description"] == original


def test_manifest_cannot_escape_the_layer_directory(tmp_path: Path) -> None:
    copied = _copy_layers(tmp_path)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["layers"].append("../customer-secret.json")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RegistryCompositionError, match="must be a file name"):
        compile_registry(_selection("ifrs_nf_en"), layer_dir=copied)


def test_runtime_shelf_contains_the_manifest_and_every_layer(monkeypatch) -> None:
    monkeypatch.setattr(station_driver, "_shelf_cache", None)
    manifest = json.loads((BUILTIN_LAYER_DIR / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        "finengine/data/registry_layers/manifest.json",
        *(f"finengine/data/registry_layers/{name}" for name in manifest["layers"]),
    }
    with zipfile.ZipFile(io.BytesIO(station_driver._build_shelf())) as shelf:
        names = {name.replace("\\", "/") for name in shelf.namelist()}
    assert expected <= names


def test_running_passport_keeps_its_frozen_registry_when_new_version_appears(
    tmp_path: Path,
) -> None:
    copied = _copy_layers(tmp_path)
    classification = classification_from_cell("ifrs_nf_en", customer_id="acme")
    old = freeze_registry(RegistrySelection.from_classification(classification), layer_dir=copied)
    passport = RunPassport(
        goal_ir_id="goal-1",
        classification=classification,
        entity=EntityIdentity(
            entity_id="entity-acme", canonical_name="ACME", raw_name="ACME",
            entity_type="company", source="test", confidence_low=False,
        ),
        registry_version=old.version,
        methodology_version="method-1",
        model_version="model-1",
        prompt_version="prompt-1",
        sandbox_image_version="sandbox-1",
    )
    _add_layer(copied, _customer_layer("customer.acme", "acme", alias="new alias"))
    new = freeze_registry(RegistrySelection.from_classification(classification), layer_dir=copied)

    assert new.version != old.version
    assert registry_for_passport(passport).fingerprint == old.fingerprint
    assert registry_for_passport(passport).fingerprint != new.fingerprint


def test_frozen_registry_is_deeply_immutable() -> None:
    compiled = freeze_registry(_selection("ifrs_nf_en"))
    aliases = compiled.metric("extractable", "TOTAL_ASSETS").fields["aliases_en"]
    assert isinstance(aliases, tuple)
    with pytest.raises(AttributeError):
        aliases.append("cannot mutate")


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_id_frame_axes_select_the_correct_compiled_profile(profile_id: str) -> None:
    classification = classification_from_cell(profile_id)
    compiled = compile_registry(RegistrySelection.from_classification(classification))
    assert compiled.profile_id == profile_id
    assert compiled.to_effective_data() == _golden()[profile_id]


def test_id_frame_override_axes_are_not_rederived_from_profile_id() -> None:
    classification = classification_from_cell(
        "ifrs_nf_en", company_type_override="bank"
    )
    compiled = compiled_registry_for_classification(classification)
    assert compiled.to_effective_data() == _golden()["ifrs_bank_en"]


def test_customer_acceptance_has_no_literal_customer_branch_in_execution_code() -> None:
    files = [
        ROOT / "src" / "finengine" / "skills" / "registry_composition.py",
        ROOT / "src" / "finengine" / "skills" / "atoms" / "graph_atoms.py",
        ROOT / "src" / "finengine" / "skills" / "atoms" / "calc_formula.py",
    ]
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names = {
                part.id if isinstance(part, ast.Name) else part.attr
                for part in [node.left, *node.comparators]
                if isinstance(part, (ast.Name, ast.Attribute))
            }
            literals = [part.value for part in [node.left, *node.comparators] if isinstance(part, ast.Constant)]
            if names & {"customer", "customer_id"} and any(isinstance(x, str) for x in literals):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"customer-specific execution branches found: {offenders}"


def test_ad_hoc_contracts_are_not_registry_layers() -> None:
    source = (ROOT / "src" / "finengine" / "skills" / "registry_composition.py").read_text(
        encoding="utf-8"
    )
    assert "AdHocConceptSpec" not in source
    assert "AdHocFormulaSpec" not in source
