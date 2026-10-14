"""Deterministic composition of the canonical metric registry (Slice 13).

The compiler owns variability; execution code only asks for an effective
registry selected by the already-frozen classification axes.  Precedence is
data in ``manifest.json`` and every winning field retains the layer/version
that supplied it.  A disagreement at one priority is rejected instead of
depending on file or registration order.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping


BUILTIN_LAYER_DIR = Path(__file__).resolve().parent.parent / "data" / "registry_layers"
MANIFEST_SCHEMA = "registry-composition-manifest/v1"
LAYER_SCHEMA = "registry-layer/v1"
LAYER_KINDS = ("standard", "company_type", "industry", "language", "customer")
SELECTOR_FIELDS = frozenset({"standard", "company_type", "industry", "language", "customer_id"})
KIND_SELECTOR_FIELD = {
    "standard": "standard",
    "company_type": "company_type",
    "industry": "industry",
    "language": "language",
    "customer": "customer_id",
}
METRIC_KINDS = ("extractable", "calculated")


class RegistryCompositionError(ValueError):
    """A layer set cannot be compiled without guessing."""


class LayerConflictError(RegistryCompositionError):
    """Two matching layers assign different values at the same priority."""


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return copy.deepcopy(value)


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class RegistrySelection:
    standard: str
    company_type: str
    language: str
    industry: str = ""
    customer_id: str = ""
    profile_id: str = ""

    @classmethod
    def from_profile_id(
        cls,
        profile_id: str,
        *,
        industry: str = "",
        customer_id: str = "",
    ) -> "RegistrySelection":
        axes_by_profile = {
            "ifrs_nf_ru": ("ifrs", "non_financial", "ru"),
            "ifrs_bank_ru": ("ifrs", "bank", "ru"),
            "rsbu_nf_ru": ("rsbu", "non_financial", "ru"),
            "rsbu_bank_ru": ("rsbu", "bank", "ru"),
            "us_gaap_nf_en": ("us_gaap", "non_financial", "en"),
            "us_gaap_bank_en": ("us_gaap", "bank", "en"),
            "ifrs_nf_en": ("ifrs", "non_financial", "en"),
            "ifrs_bank_en": ("ifrs", "bank", "en"),
            "ind_as_nf_en": ("ind_as", "non_financial", "en"),
            "ind_gaap_bank_en": ("ind_gaap", "bank", "en"),
        }
        try:
            standard, company_type, language = axes_by_profile[profile_id]
        except KeyError as exc:
            raise RegistryCompositionError(f"unknown registry profile {profile_id!r}") from exc
        return cls(
            standard=standard,
            company_type=company_type,
            language=language,
            industry=industry,
            customer_id=customer_id,
            profile_id=profile_id,
        )

    @classmethod
    def from_classification(cls, classification: Any) -> "RegistrySelection":
        return cls(
            standard=str(classification.standard),
            company_type=str(classification.company_type),
            language=str(classification.language),
            industry=str(getattr(classification, "industry", "") or ""),
            customer_id=str(getattr(classification, "customer_id", "") or ""),
            profile_id=str(classification.profile_id),
        )


@dataclass(frozen=True)
class FieldProvenance:
    layer_id: str
    version: str
    kind: str
    priority: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "version": self.version,
            "kind": self.kind,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class EffectiveMetricDef:
    metric_kind: Literal["extractable", "calculated"]
    enum: str
    fields: Mapping[str, Any]
    provenance: Mapping[str, FieldProvenance]

    def as_dict(self) -> dict[str, Any]:
        return {"enum": self.enum, **_thaw_value(self.fields)}


@dataclass(frozen=True)
class CompiledRegistry:
    profile_id: str
    precedence_version: str
    fingerprint: str
    version: str
    matched_layers: tuple[FieldProvenance, ...]
    extractable: tuple[EffectiveMetricDef, ...]
    calculated: tuple[EffectiveMetricDef, ...]
    verification_rules: Mapping[str, Any]

    def metric(self, metric_kind: str, enum: str) -> EffectiveMetricDef:
        rows = self.extractable if metric_kind == "extractable" else self.calculated
        wanted = enum.strip().upper()
        try:
            return next(row for row in rows if row.enum.strip().upper() == wanted)
        except StopIteration as exc:
            raise KeyError(f"{metric_kind}/{enum} is absent from {self.profile_id}") from exc

    def to_effective_data(self) -> dict[str, Any]:
        return {
            "extractable": [row.as_dict() for row in self.extractable],
            "calculated": [row.as_dict() for row in self.calculated],
            "verification_rules": _thaw_value(self.verification_rules),
        }


@dataclass(frozen=True)
class _Assigned:
    value: Any
    source: FieldProvenance


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryCompositionError(f"{path}: layer JSON cannot be read: {exc}") from exc


def _profile_id(selection: RegistrySelection) -> str:
    if selection.profile_id:
        return selection.profile_id
    axes = (selection.standard, selection.company_type, selection.language)
    known = {
        ("ifrs", "non_financial", "ru"): "ifrs_nf_ru",
        ("ifrs", "bank", "ru"): "ifrs_bank_ru",
        ("rsbu", "non_financial", "ru"): "rsbu_nf_ru",
        ("rsbu", "bank", "ru"): "rsbu_bank_ru",
        ("us_gaap", "non_financial", "en"): "us_gaap_nf_en",
        ("us_gaap", "bank", "en"): "us_gaap_bank_en",
        ("ifrs", "non_financial", "en"): "ifrs_nf_en",
        ("ifrs", "bank", "en"): "ifrs_bank_en",
        ("ind_as", "non_financial", "en"): "ind_as_nf_en",
        ("ind_gaap", "bank", "en"): "ind_gaap_bank_en",
    }
    try:
        return known[axes]
    except KeyError as exc:
        raise RegistryCompositionError(f"unsupported registry axes: {axes!r}") from exc


def _matches(selector: Mapping[str, Any], selection: RegistrySelection) -> bool:
    unknown = set(selector) - SELECTOR_FIELDS
    if unknown:
        raise RegistryCompositionError(f"unknown selector fields: {sorted(unknown)!r}")
    return all(str(getattr(selection, field)) == str(expected) for field, expected in selector.items())


def _load_layers(layer_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = layer_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise RegistryCompositionError(f"{manifest_path}: unsupported manifest schema")
    priorities = manifest.get("priorities")
    if not isinstance(priorities, dict) or set(priorities) != set(LAYER_KINDS):
        raise RegistryCompositionError(
            f"{manifest_path}: priorities must define exactly {list(LAYER_KINDS)!r}"
        )
    if len(set(priorities.values())) != len(priorities):
        raise RegistryCompositionError(f"{manifest_path}: layer-kind priorities must be unique")
    filenames = manifest.get("layers")
    if not isinstance(filenames, list) or not all(isinstance(name, str) for name in filenames):
        raise RegistryCompositionError(f"{manifest_path}: layers must be a list of file names")
    if len(filenames) != len(set(filenames)):
        raise RegistryCompositionError(f"{manifest_path}: duplicate layer file")

    layers: list[dict[str, Any]] = []
    ids: set[str] = set()
    for filename in sorted(filenames):
        if Path(filename).name != filename:
            raise RegistryCompositionError(f"{manifest_path}: layer path must be a file name: {filename!r}")
        path = layer_dir / filename
        layer = _read_json(path)
        if not isinstance(layer, dict) or layer.get("schema_version") != LAYER_SCHEMA:
            raise RegistryCompositionError(f"{path}: unsupported layer schema")
        layer_id = layer.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            raise RegistryCompositionError(f"{path}: non-empty layer_id is required")
        if layer_id in ids:
            raise RegistryCompositionError(f"{path}: duplicate layer_id {layer_id!r}")
        ids.add(layer_id)
        kind = layer.get("kind")
        if kind not in LAYER_KINDS:
            raise RegistryCompositionError(f"{path}: unknown layer kind {kind!r}")
        if layer.get("priority") != priorities[kind]:
            raise RegistryCompositionError(
                f"{path}: priority {layer.get('priority')!r} disagrees with "
                f"{manifest.get('precedence_version')}/{kind}={priorities[kind]}"
            )
        if not isinstance(layer.get("version"), str) or not layer["version"]:
            raise RegistryCompositionError(f"{path}: non-empty version is required")
        selector = layer.get("selector", {})
        if not isinstance(selector, dict):
            raise RegistryCompositionError(f"{path}: selector must be an object")
        required_selector = KIND_SELECTOR_FIELD[kind]
        if not str(selector.get(required_selector) or "").strip():
            raise RegistryCompositionError(
                f"{path}: {kind} layer must select a non-empty {required_selector}"
            )
        if not isinstance(layer.get("metrics", {}), dict):
            raise RegistryCompositionError(f"{path}: metrics must be an object")
        layers.append(layer)
    return manifest, layers


def _assign(
    state: dict[str, dict[str, dict[str, _Assigned]]],
    *,
    metric_kind: str,
    enum: str,
    field: str,
    value: Any,
    source: FieldProvenance,
) -> None:
    fields = state[metric_kind].setdefault(enum, {})
    previous = fields.get(field)
    if previous is not None and previous.source.priority == source.priority:
        if previous.value != value:
            raise LayerConflictError(
                f"{metric_kind}/{enum}.{field}: two values at the same priority "
                f"{source.priority}: {previous.source.layer_id}={previous.value!r}, "
                f"{source.layer_id}={value!r}"
            )
        # Identical declarations are harmless; make the chosen provenance
        # independent of manifest/file order.
        if (source.layer_id, source.version) < (
            previous.source.layer_id,
            previous.source.version,
        ):
            fields[field] = _Assigned(copy.deepcopy(value), source)
        return
    if previous is None or previous.source.priority < source.priority:
        fields[field] = _Assigned(copy.deepcopy(value), source)


def _normalize_metric(
    metric_kind: str,
    enum: str,
    assigned: dict[str, _Assigned],
    introduced_by: FieldProvenance,
) -> EffectiveMetricDef:
    # Lazy import avoids a module cycle: metric_schema delegates loading to
    # this compiler, while its existing Pydantic definitions remain the one
    # validation contract for every consumer.
    from finengine.skills.metric_schema import CalcDef, MetricDef  # noqa: PLC0415

    raw = {"enum": enum, **{field: item.value for field, item in assigned.items()}}
    model = MetricDef.model_validate(raw) if metric_kind == "extractable" else CalcDef.model_validate(raw)
    normalized = model.model_dump()
    normalized.pop("enum")
    provenance = {
        field: assigned[field].source if field in assigned else introduced_by
        for field in normalized
    }
    return EffectiveMetricDef(
        metric_kind=metric_kind,
        enum=enum,
        fields=MappingProxyType({field: _freeze_value(value) for field, value in normalized.items()}),
        provenance=MappingProxyType(provenance),
    )


def compile_registry(
    selection: RegistrySelection,
    *,
    layer_dir: Path | str | None = None,
) -> CompiledRegistry:
    """Compile one immutable effective registry for already-resolved axes."""
    root = Path(layer_dir) if layer_dir is not None else BUILTIN_LAYER_DIR
    manifest, all_layers = _load_layers(root)
    matching = [layer for layer in all_layers if _matches(layer.get("selector", {}), selection)]
    matching.sort(key=lambda layer: (int(layer["priority"]), str(layer["layer_id"])))
    if not any(layer["kind"] == "standard" for layer in matching):
        raise RegistryCompositionError(
            f"no base reporting-standard layer matches {selection.standard!r}"
        )

    state: dict[str, dict[str, dict[str, _Assigned]]] = {
        "extractable": {},
        "calculated": {},
    }
    introduced: dict[tuple[str, str], FieldProvenance] = {}
    order_value: dict[str, _Assigned] = {}
    matched_sources: list[FieldProvenance] = []

    for layer in matching:
        source = FieldProvenance(
            layer_id=layer["layer_id"],
            version=layer["version"],
            kind=layer["kind"],
            priority=int(layer["priority"]),
        )
        matched_sources.append(source)
        metrics = layer.get("metrics", {})
        unknown_metric_kinds = set(metrics) - set(METRIC_KINDS)
        if unknown_metric_kinds:
            raise RegistryCompositionError(
                f"{source.layer_id}: unknown metric kinds {sorted(unknown_metric_kinds)!r}"
            )
        for metric_kind in METRIC_KINDS:
            patches = metrics.get(metric_kind, [])
            if not isinstance(patches, list):
                raise RegistryCompositionError(f"{source.layer_id}: {metric_kind} must be a list")
            seen_in_layer: set[tuple[str, str]] = set()
            for patch in patches:
                if not isinstance(patch, dict):
                    raise RegistryCompositionError(f"{source.layer_id}: metric patch must be an object")
                enum = str(patch.get("enum") or "").strip()
                fields = patch.get("fields")
                if not enum or not isinstance(fields, dict) or not fields:
                    raise RegistryCompositionError(
                        f"{source.layer_id}: metric patch needs enum and non-empty fields"
                    )
                introduced.setdefault((metric_kind, enum), source)
                for field, value in fields.items():
                    duplicate = (enum, field)
                    if duplicate in seen_in_layer:
                        raise RegistryCompositionError(
                            f"{source.layer_id}: duplicate {metric_kind}/{enum}.{field}"
                        )
                    seen_in_layer.add(duplicate)
                    _assign(
                        state,
                        metric_kind=metric_kind,
                        enum=enum,
                        field=field,
                        value=value,
                        source=source,
                    )
        orders = layer.get("orders", {})
        if not isinstance(orders, dict) or set(orders) - set(METRIC_KINDS):
            raise RegistryCompositionError(f"{source.layer_id}: invalid orders object")
        for metric_kind, order in orders.items():
            if not isinstance(order, list) or not all(isinstance(enum, str) and enum for enum in order):
                raise RegistryCompositionError(f"{source.layer_id}: invalid {metric_kind} order")
            if len(order) != len(set(order)):
                raise RegistryCompositionError(f"{source.layer_id}: duplicate enum in {metric_kind} order")
            previous = order_value.get(metric_kind)
            if previous is not None and previous.source.priority == source.priority and previous.value != order:
                raise LayerConflictError(
                    f"{metric_kind} order: two values at the same priority {source.priority}: "
                    f"{previous.source.layer_id} and {source.layer_id}"
                )
            if previous is None or previous.source.priority < source.priority:
                order_value[metric_kind] = _Assigned(list(order), source)

    normalized: dict[str, dict[str, EffectiveMetricDef]] = {kind: {} for kind in METRIC_KINDS}
    for metric_kind in METRIC_KINDS:
        for enum, fields in state[metric_kind].items():
            normalized[metric_kind][enum] = _normalize_metric(
                metric_kind, enum, fields, introduced[(metric_kind, enum)]
            )

    ordered_rows: dict[str, tuple[EffectiveMetricDef, ...]] = {}
    for metric_kind in METRIC_KINDS:
        declared = list(order_value.get(metric_kind, _Assigned([], matched_sources[0])).value)
        absent = sorted(set(declared) - set(normalized[metric_kind]))
        if absent:
            raise RegistryCompositionError(
                f"{metric_kind} order references absent definitions: {absent!r}"
            )
        missing = sorted(
            set(normalized[metric_kind]) - set(declared),
            key=lambda enum: (
                introduced[(metric_kind, enum)].priority,
                introduced[(metric_kind, enum)].layer_id,
                enum,
            ),
        )
        ordered_rows[metric_kind] = tuple(
            normalized[metric_kind][enum] for enum in [*declared, *missing]
        )

    fingerprint_payload = {
        "precedence_version": manifest.get("precedence_version"),
        "extractable": [
            {
                **row.as_dict(),
                "_provenance": {
                    field: source.as_dict() for field, source in sorted(row.provenance.items())
                },
            }
            for row in ordered_rows["extractable"]
        ],
        "calculated": [
            {
                **row.as_dict(),
                "_provenance": {
                    field: source.as_dict() for field, source in sorted(row.provenance.items())
                },
            }
            for row in ordered_rows["calculated"]
        ],
        "verification_rules": {},
    }
    fingerprint = _canonical_digest(fingerprint_payload)
    profile_id = _profile_id(selection)
    return CompiledRegistry(
        profile_id=profile_id,
        precedence_version=str(manifest.get("precedence_version") or ""),
        fingerprint=fingerprint,
        version=f"{profile_id}:{fingerprint}",
        matched_layers=tuple(matched_sources),
        extractable=ordered_rows["extractable"],
        calculated=ordered_rows["calculated"],
        verification_rules=MappingProxyType({}),
    )


def compiled_registry_from_effective_snapshot(
    selection: RegistrySelection,
    data: Mapping[str, Any],
    *,
    layer_id: str,
    layer_version: str,
    precedence_version: str = "historical-snapshot-adapter/v1",
) -> CompiledRegistry:
    """Freeze historical shelf bytes for fingerprint/rollback comparison.

    This is not a runtime registry selector: active definitions are compiled
    only by :func:`freeze_registry`. The adapter lets rollback audits compare
    an old immutable snapshot without routing traffic through it.
    """
    source = FieldProvenance(
        layer_id=layer_id,
        version=layer_version,
        kind="customer",
        priority=400,
    )
    rows: dict[str, tuple[EffectiveMetricDef, ...]] = {}
    for metric_kind in METRIC_KINDS:
        effective: list[EffectiveMetricDef] = []
        raw_rows = data.get(metric_kind, [])
        if not isinstance(raw_rows, list):
            raise RegistryCompositionError(f"{layer_id}: {metric_kind} must be a list")
        for raw in raw_rows:
            if not isinstance(raw, dict) or not raw.get("enum"):
                raise RegistryCompositionError(f"{layer_id}: invalid {metric_kind} row")
            enum = str(raw["enum"])
            assigned = {
                field: _Assigned(copy.deepcopy(value), source)
                for field, value in raw.items()
                if field != "enum"
            }
            effective.append(_normalize_metric(metric_kind, enum, assigned, source))
        rows[metric_kind] = tuple(effective)
    verification_rules = copy.deepcopy(dict(data.get("verification_rules", {})))
    fingerprint_payload = {
        "precedence_version": precedence_version,
        "extractable": [
            {
                **row.as_dict(),
                "_provenance": {
                    field: provenance.as_dict()
                    for field, provenance in sorted(row.provenance.items())
                },
            }
            for row in rows["extractable"]
        ],
        "calculated": [
            {
                **row.as_dict(),
                "_provenance": {
                    field: provenance.as_dict()
                    for field, provenance in sorted(row.provenance.items())
                },
            }
            for row in rows["calculated"]
        ],
        "verification_rules": verification_rules,
    }
    fingerprint = _canonical_digest(fingerprint_payload)
    profile_id = _profile_id(selection)
    return CompiledRegistry(
        profile_id=profile_id,
        precedence_version=precedence_version,
        fingerprint=fingerprint,
        version=f"{profile_id}:{fingerprint}",
        matched_layers=(source,),
        extractable=rows["extractable"],
        calculated=rows["calculated"],
        verification_rules=_freeze_value(verification_rules),
    )


_FROZEN: dict[str, CompiledRegistry] = {}


def remember_compiled_registry(compiled: CompiledRegistry) -> CompiledRegistry:
    """Retain an already-compiled immutable snapshot by version."""
    _FROZEN.setdefault(compiled.version, compiled)
    return _FROZEN[compiled.version]


def freeze_registry(
    selection: RegistrySelection,
    *,
    layer_dir: Path | str | None = None,
) -> CompiledRegistry:
    """Compile and retain the exact snapshot addressed by its version."""
    compiled = compile_registry(selection, layer_dir=layer_dir)
    return remember_compiled_registry(compiled)


def registry_for_passport(passport: Any) -> CompiledRegistry:
    """Resolve registered definitions from the passport's frozen snapshot.

    If a passport arrives in a fresh process, the current layers may be used
    only when they reproduce the exact recorded version.  Silent substitution
    of a newer registry is forbidden.
    """
    version = str(passport.registry_version)
    frozen = _FROZEN.get(version)
    if frozen is not None:
        return frozen
    current = compile_registry(RegistrySelection.from_classification(passport.classification))
    if current.version != version:
        raise RegistryCompositionError(
            f"frozen registry {version!r} is unavailable; current effective version is "
            f"{current.version!r}"
        )
    _FROZEN[version] = current
    return current
