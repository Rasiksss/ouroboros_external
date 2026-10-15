"""F1 — заморозка ouroboros (ADR-0019, ADR-0030).

Со дня тега `base/ouroboros-1.5.1` изменены могут быть только `aifa/`, `aifa-symbiosis/` и
`skills/aifa-*`. Исключение — строка `путь ADR-NNNN` в `core_freeze_exceptions.txt`.
Смотрим коммиты тега..HEAD и незакоммиченные правки отслеживаемых файлов: ворота идут до коммита.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from arch_support import (
    BASE_TAG,
    REPO_ROOT,
    commit_files,
    git,
    load_core_freeze_exceptions,
    make_repo,
    missing_base_message,
    ref_exists,
    split_nul,
    write_files,
)

_MAY_CHANGE = re.compile(r"^(aifa/|aifa-symbiosis/|skills/aifa-[^/]+/)")


def changed_since(repo: Path, base: str) -> list[str]:
    # --no-renames: переименование ouroboros-файла в aifa/ не должно прятать удаление старого пути.
    out = git(repo, "diff", "--name-only", "-z", "--no-renames", base)
    return sorted(split_nul(out))


def core_freeze_violations(
    repo: Path, exceptions: dict[str, str], base: str = BASE_TAG
) -> list[str]:
    if not ref_exists(repo, base):
        return [missing_base_message("F1", base)]
    return [
        f"F1 (заморозка ouroboros, ADR-0019, ADR-0030): `{path}` изменён относительно `{base}`, "
        "а править можно только aifa/, aifa-symbiosis/ и skills/aifa-*/. Нужна правка ядра — это "
        "вопрос владельцу и новая ADR; исключение — строка `путь ADR-NNNN` в "
        "aifa/tests/architecture/core_freeze_exceptions.txt."
        for path in changed_since(repo, base)
        if not _MAY_CHANGE.match(path) and path not in exceptions
    ]


def test_ouroboros_core_is_untouched_since_the_base_tag() -> None:
    """F1: на дереве репозитория нет правок ядра ouroboros."""
    problems = core_freeze_violations(REPO_ROOT, load_core_freeze_exceptions())
    assert not problems, "\n".join(problems)


def test_f1_catches_an_edited_ouroboros_file(tmp_path: Path) -> None:
    """F1: нарушитель — правка файла ouroboros после тега."""
    repo = make_repo(tmp_path, {"ouroboros/core.py": "x = 1\n", "aifa/a.py": "\n"})
    commit_files(repo, {"ouroboros/core.py": "x = 2\n"}, "edit core")
    problems = core_freeze_violations(repo, {})
    assert len(problems) == 1
    assert "ouroboros/core.py" in problems[0]
    assert "ADR-0019" in problems[0]
    assert "ADR-0030" in problems[0]


def test_f1_catches_an_uncommitted_edit(tmp_path: Path) -> None:
    """F1: правка в рабочем дереве ловится до коммита."""
    repo = make_repo(tmp_path, {"pyproject.toml": "a\n"})
    write_files(repo, {"pyproject.toml": "b\n"})
    assert "pyproject.toml" in "\n".join(core_freeze_violations(repo, {}))


def test_f1_catches_a_new_file_outside_the_allowed_paths(tmp_path: Path) -> None:
    """F1: нарушитель — новый файл в корне и в чужом навыке `skills/`."""
    repo = make_repo(tmp_path, {"README.md": "r\n"})
    commit_files(repo, {"notes.md": "n\n", "skills/other/SKILL.md": "s\n"}, "add")
    text = "\n".join(core_freeze_violations(repo, {}))
    assert "`notes.md`" in text
    assert "skills/other/SKILL.md" in text


def test_f1_catches_a_deleted_ouroboros_file(tmp_path: Path) -> None:
    """F1: удаление файла ядра — тоже изменение."""
    repo = make_repo(tmp_path, {"supervisor/queue.py": "q\n"})
    commit_files(repo, {"supervisor/queue.py": None}, "drop")
    assert "supervisor/queue.py" in "\n".join(core_freeze_violations(repo, {}))


def test_f1_does_not_let_a_move_into_aifa_hide_the_deleted_core_path(tmp_path: Path) -> None:
    """F1: «перенос» файла ядра в aifa/ оставляет нарушением удаление исходного пути."""
    repo = make_repo(tmp_path, {"supervisor/queue.py": "q = 1\n"})
    commit_files(repo, {"supervisor/queue.py": None, "aifa/queue.py": "q = 1\n"}, "move")
    assert "supervisor/queue.py" in "\n".join(core_freeze_violations(repo, {}))


def test_f1_allows_aifa_symbiosis_docs_and_aifa_skills(tmp_path: Path) -> None:
    """F1: разрешённые пути не дают нарушений."""
    repo = make_repo(tmp_path, {"README.md": "r\n"})
    commit_files(
        repo,
        {
            "aifa/finengine/x.py": "\n",
            "aifa-symbiosis/plan/PROGRESS.md": "p\n",
            "skills/aifa-engine/plugin.py": "\n",
            "skills/aifa-planning/SKILL.md": "s\n",
        },
        "allowed",
    )
    assert core_freeze_violations(repo, {}) == []


def test_f1_exception_with_an_adr_lets_the_path_through(tmp_path: Path) -> None:
    """F1: путь из файла исключений не считается нарушением."""
    repo = make_repo(tmp_path, {".dockerignore": "a\n"})
    commit_files(repo, {".dockerignore": "a\naifa/\n"}, "ignore aifa")
    assert core_freeze_violations(repo, {".dockerignore": "ADR-0020"}) == []


def test_f1_fails_loudly_when_the_base_tag_is_missing(tmp_path: Path) -> None:
    """F1: без тега базы проверка падает, а не пропускается."""
    repo = make_repo(tmp_path, {"README.md": "r\n"}, tag=None)
    problems = core_freeze_violations(repo, {})
    assert len(problems) == 1
    assert BASE_TAG in problems[0]
    assert "ADR-0030" in problems[0]


def test_f1_exception_file_rejects_a_line_without_an_adr(tmp_path: Path) -> None:
    """F1: у каждого исключения есть номер ADR."""
    exceptions = tmp_path / "exceptions.txt"
    exceptions.write_text("# комментарий\n\n.dockerignore\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"ADR-NNNN"):
        load_core_freeze_exceptions(exceptions, REPO_ROOT)


def test_f1_exception_file_rejects_an_unknown_adr(tmp_path: Path) -> None:
    """F1: номер ADR должен указывать на существующую запись."""
    exceptions = tmp_path / "exceptions.txt"
    exceptions.write_text(".dockerignore ADR-9999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ADR-9999"):
        load_core_freeze_exceptions(exceptions, REPO_ROOT)


def test_f1_exception_file_accepts_an_existing_adr(tmp_path: Path) -> None:
    """F1: строка `путь ADR-NNNN` с существующей записью читается."""
    exceptions = tmp_path / "exceptions.txt"
    exceptions.write_text("# ok\n.dockerignore ADR-0020\n", encoding="utf-8")
    assert load_core_freeze_exceptions(exceptions, REPO_ROOT) == {".dockerignore": "ADR-0020"}
