"""Navigator structure catalog for configured model nIDs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import api, constants as catalog
from .api import NavigatorConfig


@dataclass(frozen=True)
class NavigatorModelInfo:
    n_id: int
    s_name: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class StructureCatalog:
    by_normalized_sname: dict[str, NavigatorModelInfo]
    by_nid: dict[int, NavigatorModelInfo]
    models_in_order: tuple[NavigatorModelInfo, ...]

    @classmethod
    def fetch(cls, settings: NavigatorConfig, *, deadline: api.NavigatorDeadline | None = None) -> StructureCatalog:
        model_ids = api.configured_model_ids(settings)
        payload = api.post_structure(settings, model_ids, deadline=deadline)
        return parse_structure_payload(payload)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StructureCatalog:
        return parse_structure_payload(payload)


def parse_structure_payload(payload: dict[str, Any]) -> StructureCatalog:
    by_normalized_sname: dict[str, NavigatorModelInfo] = {}
    by_nid: dict[int, NavigatorModelInfo] = {}
    models_in_order: list[NavigatorModelInfo] = []

    for node in payload.get("model") or []:
        if not isinstance(node, dict):
            continue
        n_id_raw = node.get("nID")
        if n_id_raw is None:
            continue
        try:
            n_id = int(n_id_raw)
        except (TypeError, ValueError):
            continue

        s_name_raw = node.get("sName")
        s_name = str(s_name_raw).strip() if s_name_raw is not None else ""
        if not s_name:
            s_name = str(n_id)

        info = NavigatorModelInfo(n_id=n_id, s_name=s_name, raw=node)
        normalized = catalog.normalize_model_key(info.s_name)
        by_normalized_sname[normalized] = info
        by_nid[n_id] = info
        models_in_order.append(info)

    return StructureCatalog(
        by_normalized_sname=by_normalized_sname,
        by_nid=by_nid,
        models_in_order=tuple(models_in_order),
    )


def catalog_entries(structure_catalog: StructureCatalog) -> list[dict[str, object]]:
    return [
        {
            "sName": info.s_name,
            "nID": info.n_id,
            "metadata_status": "ready",
        }
        for info in structure_catalog.models_in_order
    ]


def resolve_model_input(
    model: str,
    structure_catalog: StructureCatalog,
) -> NavigatorModelInfo:
    text = str(model or "").strip()
    if not text:
        raise ValueError("empty model")

    if text.isdigit():
        info = structure_catalog.by_nid.get(int(text))
        if info is None:
            raise ValueError(f"unknown nID: {text}")
        return info

    normalized = catalog.normalize_model_key(text)
    matches = [info for key, info in structure_catalog.by_normalized_sname.items() if key == normalized]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(info.s_name for info in matches)
        raise ValueError(f"ambiguous: {names}")
    raise ValueError(f"unknown: {text}")


def columns_from_model_node(model_node: dict[str, Any]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    table = model_node.get("table") or {}
    if not isinstance(table, dict):
        return columns
    for column in table.get("column") or []:
        if not isinstance(column, dict):
            continue
        s_name = column.get("sName")
        if not s_name:
            continue
        descriptor: dict[str, Any] = {"sName": str(s_name)}
        for key in ("sDescription", "sDataType", "sExample"):
            if column.get(key) is not None:
                descriptor[key] = column[key]
        columns.append(descriptor)
    return columns


def column_names_from_model_node(model_node: dict[str, Any]) -> set[str]:
    columns: set[str] = set()
    table = model_node.get("table") or {}
    if not isinstance(table, dict):
        return columns
    for column in table.get("column") or []:
        if not isinstance(column, dict):
            continue
        s_name = column.get("sName")
        if s_name:
            columns.add(str(s_name))
    return columns
