"""F13 — дисциплина ADR (ADR-0001).

Если в диапазоне изменены `ARCHITECTURE.md`, `CONTRACTS.md`, `FITNESS.md` или
`aifa/contracts/*.snapshot.json`, в нём же есть новая ADR или изменена строка статуса существующей.
У принятой ADR меняется только строка статуса; принятые записи не удаляются.

Диапазон — `AIFA_ADR_RANGE=<база>..<конец>`; по умолчанию — от `git merge-base dev HEAD` до
рабочего дерева (коммиты ветки шага и ещё не закоммиченные правки; новый неотслеживаемый файл ADR
считается). Конец, названный явно, — чистый диапазон коммитов.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from arch_support import (
    ADR_DIR,
    REPO_ROOT,
    GitError,
    commit_files,
    git,
    make_repo,
    ref_exists,
    split_nul,
    write_files,
)

ARCH_DIR = "aifa-symbiosis/architecture"
GOVERNED_DOCS = frozenset(
    f"{ARCH_DIR}/{name}" for name in ("ARCHITECTURE.md", "CONTRACTS.md", "FITNESS.md")
)
SNAPSHOT = re.compile(r"^aifa/contracts/[^/]+\.snapshot\.json$")
ADR_FILE = re.compile(rf"^{re.escape(ADR_DIR)}/\d{{4}}-[^/]+\.md$")
STATUS = "Статус:"
PROPOSED = "предложено"


def resolve_range(repo: Path, spec: str | None) -> tuple[str, str | None] | str:
    """(база, конец или None = рабочее дерево) либо текст ошибки."""
    if spec:
        base, _, end = spec.partition("..")
        return base, end or None
    for trunk in ("dev", "origin/dev"):
        if ref_exists(repo, trunk):
            return git(repo, "merge-base", trunk, "HEAD").strip(), None
    return (
        "F13 (ADR-0001): нет ветки `dev`, от которой считать диапазон шага. Задайте диапазон "
        "явно: AIFA_ADR_RANGE=<база>..<конец> (например, `17b3f93..HEAD`)."
    )


def changed_paths(repo: Path, base: str, end: str | None) -> dict[str, str]:
    refs = [base] if end is None else [base, end]
    out = split_nul(git(repo, "diff", "--name-status", "-z", "--no-renames", *refs))
    changes = {path: code[0] for code, path in zip(out[0::2], out[1::2], strict=True)}
    if end is None:
        untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", ADR_DIR)
        changes.update({path: "A" for path in split_nul(untracked)})
    return changes


def _text_at(repo: Path, ref: str | None, path: str) -> str:
    if ref is None:
        return (repo / path).read_text(encoding="utf-8")
    return git(repo, "show", f"{ref}:{path}")


def _without_status(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith(STATUS)]


def _status_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.startswith(STATUS)), "")


def _is_governed(path: str) -> bool:
    return path in GOVERNED_DOCS or bool(SNAPSHOT.match(path))


def adr_discipline_violations(repo: Path, base: str, end: str | None = None) -> list[str]:
    changes = changed_paths(repo, base, end)
    problems: list[str] = []
    status_changed: list[str] = []
    for path, code in sorted(changes.items()):
        if not ADR_FILE.match(path) or code == "A":
            continue
        before = _text_at(repo, base, path)
        if _status_line(before).removeprefix(STATUS).strip().startswith(PROPOSED):
            if code == "M" and _status_line(before) != _status_line(_text_at(repo, end, path)):
                status_changed.append(path)
            continue
        if code == "D":
            problems.append(
                f"F13 (ADR-0001): принятая запись {path} удалена. Принятые ADR не удаляют: "
                "решение меняется новой записью, у старой — строка статуса «заменено ADR-NNNN»."
            )
            continue
        after = _text_at(repo, end, path)
        if _without_status(before) != _without_status(after):
            problems.append(
                f"F13 (ADR-0001): в принятой записи {path} изменено не только строка статуса. "
                "Принятую ADR не переписывают: решение меняется новой записью "
                "(adr/README.md, шаблон)."
            )
        elif _status_line(before) != _status_line(after):
            status_changed.append(path)

    added = [p for p, code in changes.items() if ADR_FILE.match(p) and code == "A"]
    governed = sorted(p for p in changes if _is_governed(p))
    if governed and not added and not status_changed:
        problems.append(
            "F13 (ADR-0001): изменены " + ", ".join(governed) + ", а нового файла "
            f"{ADR_DIR}/NNNN-*.md или изменения строки статуса существующей записи в диапазоне "
            "нет. Архитектура, контракты и проверки меняются только вместе с ADR."
        )
    return problems


def current_violations(spec: str | None) -> list[str]:
    resolved = resolve_range(REPO_ROOT, spec)
    if isinstance(resolved, str):
        return [resolved]
    return adr_discipline_violations(REPO_ROOT, *resolved)


def test_architecture_changes_come_with_an_adr() -> None:
    """F13: диапазон шага (или AIFA_ADR_RANGE) соблюдает дисциплину ADR."""
    problems = current_violations(os.environ.get("AIFA_ADR_RANGE") or None)
    assert not problems, "\n".join(problems)


ACCEPTED = f"# ADR-0100. Тест\n\n{STATUS} принято (14.10.2026)\n\n## Решение\nОдно.\n"
PROPOSED_ADR = f"# ADR-0101. Тест\n\n{STATUS} предложено\n\n## Решение\nОдно.\n"
ADR_PATH = f"{ADR_DIR}/0100-test.md"
PROPOSED_PATH = f"{ADR_DIR}/0101-test.md"
ARCH_PATH = f"{ARCH_DIR}/ARCHITECTURE.md"
SNAPSHOT_PATH = "aifa/contracts/tools.snapshot.json"


def _step_repo(tmp_path: Path) -> Path:
    return make_repo(
        tmp_path,
        {
            ARCH_PATH: "# arch\n",
            f"{ARCH_DIR}/CONTRACTS.md": "# contracts\n",
            SNAPSHOT_PATH: "{}\n",
            ADR_PATH: ACCEPTED,
            PROPOSED_PATH: PROPOSED_ADR,
        },
        tag=None,
    )


def _base(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def test_f13_catches_an_architecture_change_without_an_adr(tmp_path: Path) -> None:
    """F13: нарушитель — правка ARCHITECTURE.md без записи ADR."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    commit_files(repo, {ARCH_PATH: "# arch\nновое\n"}, "arch")
    problems = adr_discipline_violations(repo, base, "HEAD")
    assert len(problems) == 1
    assert ARCH_PATH in problems[0]
    assert "ADR-0001" in problems[0]


def test_f13_catches_a_snapshot_change_without_an_adr(tmp_path: Path) -> None:
    """F13: нарушитель — правка снимка контракта без записи ADR."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    commit_files(repo, {SNAPSHOT_PATH: '{"a": 1}\n'}, "snapshot")
    assert SNAPSHOT_PATH in "\n".join(adr_discipline_violations(repo, base, "HEAD"))


def test_f13_accepts_the_change_with_a_new_adr(tmp_path: Path) -> None:
    """F13: правка архитектуры в диапазоне с новой ADR — зелёная."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    new_adr = f"{ADR_DIR}/0102-new.md"
    commit_files(repo, {ARCH_PATH: "# arch\nновое\n"}, "arch")
    commit_files(repo, {new_adr: f"# ADR-0102\n\n{STATUS} принято\n"}, "adr")
    assert adr_discipline_violations(repo, base, "HEAD") == []


def test_f13_accepts_the_change_with_a_status_line_change(tmp_path: Path) -> None:
    """F13: строка статуса существующей ADR как основание — зелёная."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    replaced = ACCEPTED.replace("принято (14.10.2026)", "заменено ADR-0102")
    commit_files(repo, {ARCH_PATH: "# arch\nновое\n", ADR_PATH: replaced}, "supersede")
    assert adr_discipline_violations(repo, base, "HEAD") == []


def test_f13_catches_a_rewritten_accepted_adr(tmp_path: Path) -> None:
    """F13: нарушитель — у принятой ADR изменено решение, а не только статус."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    commit_files(repo, {ADR_PATH: ACCEPTED.replace("Одно.", "Другое.")}, "rewrite")
    problems = adr_discipline_violations(repo, base, "HEAD")
    assert len(problems) == 1
    assert ADR_PATH in problems[0]
    assert "только строка статуса" in problems[0]


def test_f13_catches_a_deleted_accepted_adr(tmp_path: Path) -> None:
    """F13: нарушитель — удалена принятая ADR."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    commit_files(repo, {ADR_PATH: None}, "drop")
    assert "удалена" in "\n".join(adr_discipline_violations(repo, base, "HEAD"))


def test_f13_allows_editing_a_proposed_adr(tmp_path: Path) -> None:
    """F13: предложенную (ещё не принятую) запись править можно."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    commit_files(repo, {PROPOSED_PATH: PROPOSED_ADR.replace("Одно.", "Лучше.")}, "refine")
    assert adr_discipline_violations(repo, base, "HEAD") == []


def test_f13_sees_uncommitted_changes_and_a_new_untracked_adr(tmp_path: Path) -> None:
    """F13: ворота до коммита — рабочее дерево и неотслеживаемый файл ADR учитываются."""
    repo = _step_repo(tmp_path)
    base = _base(repo)
    write_files(repo, {ARCH_PATH: "# arch\nновое\n"})
    assert adr_discipline_violations(repo, base)
    write_files(repo, {f"{ADR_DIR}/0102-new.md": f"# ADR-0102\n\n{STATUS} предложено\n"})
    assert adr_discipline_violations(repo, base) == []


def test_f13_default_range_starts_at_the_merge_base_with_dev(tmp_path: Path) -> None:
    """F13: по умолчанию диапазон — ветка шага относительно `dev`."""
    repo = _step_repo(tmp_path)
    git(repo, "branch", "-M", "dev")
    base = _base(repo)
    git(repo, "checkout", "-q", "-b", "sym/step")
    commit_files(repo, {ARCH_PATH: "# arch\nновое\n"}, "arch")
    assert resolve_range(repo, None) == (base, None)
    assert adr_discipline_violations(repo, base)


def test_f13_range_can_be_given_as_a_parameter(tmp_path: Path) -> None:
    """F13: AIFA_ADR_RANGE `A..B` — чистый диапазон коммитов; `A..` — до рабочего дерева."""
    repo = _step_repo(tmp_path)
    assert resolve_range(repo, "abc..def") == ("abc", "def")
    assert resolve_range(repo, "abc..") == ("abc", None)


def test_f13_reports_a_missing_trunk_instead_of_passing(tmp_path: Path) -> None:
    """F13: нет ни `dev`, ни `origin/dev` — текст ошибки, а не молчаливая зелень."""
    repo = _step_repo(tmp_path)
    git(repo, "branch", "-M", "work")
    resolved = resolve_range(repo, None)
    assert isinstance(resolved, str)
    assert "AIFA_ADR_RANGE" in resolved


def test_f13_raises_a_clear_error_for_an_unknown_ref(tmp_path: Path) -> None:
    """F13: неизвестная база в диапазоне — ошибка git с текстом, а не «нет нарушений»."""
    repo = _step_repo(tmp_path)
    with pytest.raises(GitError, match="no-such-ref"):
        adr_discipline_violations(repo, "no-such-ref", "HEAD")
