"""The owner argument is required on every object-storage method and changes no key."""

from __future__ import annotations

import inspect
import pathlib

import pytest

from new_core.identity import OwnerScope
from ouroboros.storage.blob.drive import DriveStorage
from ouroboros.storage.blob.keys import build_object_key
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.blob.s3_backend import S3StorageProvider
from ouroboros.storage.blob.scopes import ALL_SCOPES, OWNER_KEYED_SCOPES
from tests.helpers.blob_double import InMemoryStorageProvider
from tests.helpers.shared import TEST_SCOPE

_OTHER_SCOPE = OwnerScope(owner_id="other", agent_id="second", surface_id="dialogs", chat_id="9")


@pytest.mark.parametrize("scope", sorted(ALL_SCOPES - OWNER_KEYED_SCOPES))
def test_object_keys_are_unchanged_by_the_owner_argument(scope):
    """The required owner argument is plumbing outside ``OWNER_KEYED_SCOPES``, not isolation."""
    key = "dir/object.json"

    assert build_object_key(TEST_SCOPE, scope, key) == build_object_key(_OTHER_SCOPE, scope, key)


_OBJECT_METHODS = {
    "exists",
    "put_text",
    "get_text",
    "put_bytes",
    "put_fileobj",
    "get_bytes",
    "copy_to_path",
    "put_json",
    "get_json",
    "create_json_if_absent",
    "delete_json_if_matches",
    "replace_json_if_matches",
    "list_keys",
    "delete",
    "atomic_put_json",
}

_DRIVE_OBJECT_METHODS = {
    "read_text_rel",
    "write_text_rel",
    "read_bytes_rel",
    "write_bytes_rel",
    "read_json_rel",
    "atomic_write_json_rel",
    "exists_rel",
    "path_exists",
    "list_entries",
    "delete_rel",
    "delete_prefix",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "read_json_dict",
    "atomic_write_json",
}


@pytest.mark.parametrize(
    ("cls", "names"),
    [
        (StorageProvider, _OBJECT_METHODS),
        (S3StorageProvider, _OBJECT_METHODS),
        (InMemoryStorageProvider, _OBJECT_METHODS),
        (DriveStorage, _DRIVE_OBJECT_METHODS),
    ],
)
def test_every_object_method_names_the_owner_first_and_without_a_default(cls, names):
    for name in sorted(names):
        signature = inspect.signature(getattr(cls, name))
        parameters = list(signature.parameters.values())
        assert [p.name for p in parameters[:2]] == ["self", "owner_scope"], f"{cls.__name__}.{name}"
        assert parameters[1].default is inspect.Parameter.empty, f"{cls.__name__}.{name}"


_FORBIDDEN_UTILS_IMPORTS = {
    "_configured_data_dir",
    "atomic_write_json",
    "drive_aware_exists",
    "read_bytes",
    "read_json_dict",
    "read_text",
    "write_bytes",
    "write_text",
}


def test_no_tool_module_reaches_storage_through_the_path_only_helpers():
    """A tool addresses the drive it was handed; those helpers resolve the configured one."""
    tools_root = pathlib.Path(__file__).resolve().parents[3] / "ouroboros" / "tools"
    offenders: list[str] = []
    for path in sorted(tools_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "ouroboros.utils" not in source:
            continue
        for name in _FORBIDDEN_UTILS_IMPORTS:
            if f"import {name}" in source or f"{name}," in source.split("ouroboros.utils import", 1)[-1][:300]:
                offenders.append(f"{path.name}:{name}")
    assert offenders == []
