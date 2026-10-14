from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros.storage.blob.keys import build_object_key, build_scope_prefix, storage_key_prefix
from ouroboros.storage.blob.scopes import ALL_SCOPES
from ouroboros.tools.core import _data_list, _data_read, _data_write
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx


def _ctx(tmp_path: pathlib.Path):
    return make_tool_ctx(repo_dir=tmp_path / "repo", drive_root=tmp_path, scope=TEST_SCOPE)


@pytest.mark.parametrize("scope", sorted(ALL_SCOPES))
def test_every_scope_keeps_the_pre_owner_segment_key(scope):
    key = "dir/object.json"

    assert build_object_key(TEST_SCOPE, scope, key) == f"{storage_key_prefix()}/{scope}/{key}"
    assert build_scope_prefix(TEST_SCOPE, scope) == f"{storage_key_prefix()}/{scope}/"


def test_data_write_reaches_a_scope_without_an_owner_in_its_key(tmp_path):
    ctx = _ctx(tmp_path)

    assert _data_write(ctx, "state/notes.txt", "note").startswith("OK")
    assert "note" in _data_read(ctx, "state/notes.txt")
    assert json.loads(_data_list(ctx, "state")) == ["notes.txt"]


def test_the_mode_is_what_turns_that_write_into_a_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    ctx = _ctx(tmp_path)

    assert "DATA_WRITE_BLOCKED" in _data_write(ctx, "state/notes.txt", "note")
