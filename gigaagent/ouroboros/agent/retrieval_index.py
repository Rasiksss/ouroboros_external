"""Tool-agnostic indexing for compact working-memory records."""
# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, Field

from ouroboros.utils import (
    sanitize_tool_args_for_log,
    sanitize_tool_result_for_log,
    truncate_review_artifact,
)

_WORD_RE = re.compile(r"[^\W_]+(?:[-.][^\W_]+)*", re.UNICODE)
_REFERENCE_KEYS = frozenset({"url", "uri", "path", "date", "datetime"})
_SECRET_KEY_PARTS = ("authorization", "credential", "password", "secret", "token", "api_key")
_STOP_WORDS = frozenset(
    {
        "было",
        "были",
        "для",
        "как",
        "когда",
        "который",
        "про",
        "что",
        "this",
        "that",
        "the",
        "with",
    }
)


class RetrievalIndex(BaseModel):
    terms: list[str] = Field(default_factory=list)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    refs: dict[str, str] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


def normalize_term(value: Any) -> str:
    term = str(value or "").strip().casefold().replace("ё", "е")
    if len(term) > 5:
        for ending in (
            "иями",
            "ями",
            "ами",
            "ого",
            "ему",
            "ому",
            "иях",
            "ах",
            "ях",
            "ой",
            "ый",
            "ая",
            "ое",
            "ам",
            "ям",
            "а",
            "я",
            "ы",
            "и",
            "у",
            "ю",
            "е",
        ):
            if term.endswith(ending) and len(term) - len(ending) >= 4:
                return term[: -len(ending)]
    return term


def tokenize(value: Any, *, limit: int = 100) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in _WORD_RE.finditer(str(value or "")):
        term = normalize_term(match.group(0))
        if len(term) < 2 or term in _STOP_WORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def merge_terms(*groups: Iterable[Any], limit: int = 100) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            term = normalize_term(value)
            if not term or term in seen:
                continue
            seen.add(term)
            merged.append(term)
            if len(merged) >= limit:
                return merged
    return merged


def _safe_result(tool_name: str, result: Any) -> Any:
    if isinstance(result, Mapping):
        return sanitize_retrieval_mapping(tool_name, result)
    if isinstance(result, list):
        return sanitize_retrieval_mapping(tool_name, {"items": result}).get(
            "items",
            [],
        )
    text = sanitize_tool_result_for_log(str(result or ""))
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    if isinstance(parsed, dict):
        return sanitize_retrieval_mapping(tool_name, parsed)
    return parsed


def sanitize_retrieval_mapping(
    tool_name: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    return _redact_secret_keys(sanitize_tool_args_for_log(tool_name, dict(value)))


def _redact_secret_keys(value: Mapping[str, Any]) -> dict[str, Any]:
    redacted = dict(value)
    for key, item in list(redacted.items()):
        normalized_key = str(key).casefold().replace("-", "_")
        if any(part in normalized_key for part in _SECRET_KEY_PARTS):
            redacted[key] = "*** REDACTED ***"
        elif isinstance(item, Mapping):
            redacted[key] = _redact_secret_keys(item)
        elif isinstance(item, list):
            redacted[key] = [_redact_secret_keys(child) if isinstance(child, Mapping) else child for child in item]
    return redacted


def _walk_values(value: Any, *, depth: int = 0) -> Iterable[tuple[str, Any]]:
    if depth > 3:
        return
    if isinstance(value, Mapping):
        for key, item in list(value.items())[:50]:
            yield str(key), item
            yield from _walk_values(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value[:50]:
            yield from _walk_values(item, depth=depth + 1)


def _references(*values: Any) -> dict[str, str]:
    refs: dict[str, str] = {}
    for value in values:
        for key, item in _walk_values(value):
            normalized_key = key.strip().casefold()
            is_reference = normalized_key in _REFERENCE_KEYS or normalized_key.endswith("_id")
            if not is_reference or isinstance(item, (dict, list)):
                continue
            text = str(item or "").strip()
            if text and "REDACTED" not in text and normalized_key not in refs:
                refs[normalized_key] = text[:500]
    return refs


def build_retrieval_index(tool_name: str, args: dict[str, Any] | None, result: Any) -> RetrievalIndex:
    name = str(tool_name or "").strip()
    safe_args = sanitize_retrieval_mapping(name, dict(args or {}))
    safe_result = _safe_result(name, result)
    serialized_args = json.dumps(safe_args, ensure_ascii=False, sort_keys=True, default=str)
    serialized_result = json.dumps(safe_result, ensure_ascii=False, sort_keys=True, default=str)
    terms = merge_terms(
        [name],
        tokenize(serialized_args),
        tokenize(serialized_result),
    )
    preview = truncate_review_artifact(
        safe_result if isinstance(safe_result, str) else serialized_result,
        limit=1000,
    )
    return RetrievalIndex(
        terms=terms,
        refs=_references(safe_args, safe_result),
        summary={"tool": name, "preview": preview},
    )


__all__ = [
    "RetrievalIndex",
    "build_retrieval_index",
    "merge_terms",
    "normalize_term",
    "sanitize_retrieval_mapping",
    "tokenize",
]
