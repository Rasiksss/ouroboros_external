"""F11 (первая половина) — замок окружения ouroboros не менялся (ADR-0020, ADR-0030).

Корневой `uv.lock` равен версии на теге `base/ouroboros-1.5.1`: окружения раздельны, общий замок
связал бы обновления двух продуктов. Новый снимок ouroboros — только по ADR и строкой `uv.lock
ADR-NNNN` в `core_freeze_exceptions.txt`. Вторая половина (в замке службы нет `mcp`) — этап 5.
"""

from __future__ import annotations

from pathlib import Path

from arch_support import (
    BASE_TAG,
    REPO_ROOT,
    commit_files,
    git,
    load_core_freeze_exceptions,
    make_repo,
    missing_base_message,
    ref_exists,
    write_files,
)

LOCK = "uv.lock"


def root_lock_violations(repo: Path, exceptions: dict[str, str], base: str = BASE_TAG) -> list[str]:
    if not ref_exists(repo, base):
        return [missing_base_message("F11", base)]
    if LOCK in exceptions:
        return []
    # hash-object применяет те же преобразования строк, что `git add`: сравниваем содержимое,
    # а не то, как его отдал checkout на этой машине.
    on_base = git(repo, "rev-parse", f"{base}:{LOCK}").strip()
    on_disk = git(repo, "hash-object", "--", LOCK).strip()
    if on_base == on_disk:
        return []
    return [
        "F11 (замок окружения ouroboros, ADR-0020, ADR-0030): "
        f"корневой {LOCK} отличается от версии на `{base}`. Окружения ouroboros и службы "
        "раздельны: замок ouroboros не правим. Новый "
        f"снимок ouroboros — только по ADR и строкой `{LOCK} ADR-NNNN` в "
        "aifa/tests/architecture/core_freeze_exceptions.txt."
    ]


def test_root_lock_equals_the_base_tag() -> None:
    """F11: корневой uv.lock равен версии на теге базы."""
    problems = root_lock_violations(REPO_ROOT, load_core_freeze_exceptions())
    assert not problems, "\n".join(problems)


def test_f11_catches_a_committed_lock_change(tmp_path: Path) -> None:
    """F11: нарушитель — коммит с изменённым корневым замком."""
    repo = make_repo(tmp_path, {LOCK: "version = 1\n"})
    commit_files(repo, {LOCK: "version = 2\n"}, "relock")
    problems = root_lock_violations(repo, {})
    assert len(problems) == 1
    assert "ADR-0020" in problems[0]
    assert "ADR-0030" in problems[0]


def test_f11_catches_an_uncommitted_lock_change(tmp_path: Path) -> None:
    """F11: правка замка в рабочем дереве ловится до коммита."""
    repo = make_repo(tmp_path, {LOCK: "version = 1\n"})
    write_files(repo, {LOCK: "version = 2\n"})
    assert root_lock_violations(repo, {})


def test_f11_accepts_an_unchanged_lock(tmp_path: Path) -> None:
    """F11: после несвязанных коммитов замок равен базе — зелёная проверка."""
    repo = make_repo(tmp_path, {LOCK: "version = 1\n"})
    commit_files(repo, {"aifa/x.py": "\n"}, "other work")
    assert root_lock_violations(repo, {}) == []


def test_f11_lets_an_adr_exception_through(tmp_path: Path) -> None:
    """F11: новый снимок ouroboros по ADR (строка в файле исключений) не нарушение."""
    repo = make_repo(tmp_path, {LOCK: "version = 1\n"})
    commit_files(repo, {LOCK: "version = 2\n"}, "snapshot")
    assert root_lock_violations(repo, {LOCK: "ADR-0030"}) == []


def test_f11_fails_loudly_when_the_base_tag_is_missing(tmp_path: Path) -> None:
    """F11: без тега базы проверка падает, а не пропускается."""
    repo = make_repo(tmp_path, {LOCK: "version = 1\n"}, tag=None)
    problems = root_lock_violations(repo, {})
    assert len(problems) == 1
    assert BASE_TAG in problems[0]
