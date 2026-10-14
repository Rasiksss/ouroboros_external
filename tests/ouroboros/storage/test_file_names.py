from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroboros.storage.attachment_validation import AttachmentError
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.files import stored_file
from tests.helpers.shared import TEST_SCOPE


def test_create_rename_and_move_allocate_free_names_without_touching_old_files():
    first = stored_file(filename="report.txt")
    second = stored_file(filename="report.txt")
    assert second["filename"] == f"report_{second['id'][:8]}.txt"
    engine = runtime_persistence.current.engine
    with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        folder = catalog.create_folder("Reports", None)
        catalog.edit_file(first["id"], folder_id=folder.id, move=True)
        catalog.edit_file(second["id"], name="report.txt")
        catalog.edit_file(second["id"], name="report.txt")
        assert catalog.file(second["id"]).name == "report.txt"
        moved = catalog.edit_file(second["id"], folder_id=folder.id, move=True)
        assert moved.name == f"report_{second['id'][:8]}.txt"
        assert catalog.file(first["id"]).name == "report.txt"


def test_concurrent_rename_checks_suffix_collisions_and_preserves_extension():
    engine = runtime_persistence.current.engine
    first, second = stored_file(), stored_file()
    target = "x" * 196 + ".txt"
    stored_file(filename=target)
    stored_file(filename=f"{'x' * 187}_{first['id'][:8]}.txt")

    def rename(file):
        with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
            return catalog.edit_file(file["id"], name=target).name

    with ThreadPoolExecutor(2) as pool:
        names = list(pool.map(rename, [first, second]))
    assert len(set(names)) == 2
    assert all(name != target and len(name) <= 200 and name.endswith(".txt") for name in names)
    assert names[0] != f"{'x' * 187}_{first['id'][:8]}.txt"


def test_rename_validates_mime_but_move_preserves_legacy_names():
    file = stored_file(filename="legacy.exe", mime="text/plain")
    engine = runtime_persistence.current.engine
    with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        folder = catalog.create_folder("Legacy", None)
        assert catalog.edit_file(file["id"], folder_id=folder.id, move=True).name == "legacy.exe"
    with engine.begin() as conn, FileCatalog(conn, TEST_SCOPE) as catalog:
        with pytest.raises(AttachmentError):
            catalog.edit_file(file["id"], name="renamed.pdf")
        assert catalog.file(file["id"]).name == "legacy.exe"
