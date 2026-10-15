"""Общее для проверок симбиоза (FITNESS.md): git, файл исключений, обход исходников.

Только стандартная библиотека (ADR-0020): проверки идут в окружении службы и читают исходники
обоих продуктов, не импортируя их.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import warnings
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_TAG = "base/ouroboros-1.5.1"
ADR_DIR = "aifa-symbiosis/architecture/adr"
DEV_DIR = REPO_ROOT / "aifa" / "deploy" / "dev"
EXCEPTIONS_FILE = Path(__file__).with_name("core_freeze_exceptions.txt")

_SKIP_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", ".next", "venv"})
_SKIP_DIR_PREFIXES = (".venv", ".mypy_cache", ".ruff_cache", ".pytest_cache")
_EXCEPTION_LINE = re.compile(r"^(?P<path>\S+)\s+(?P<adr>ADR-\d{4})$")


class GitError(RuntimeError):
    """git вернул ошибку; текст — его stderr."""


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if done.returncode != 0:
        stderr = done.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)}: {stderr}")
    return done.stdout.decode("utf-8")


def ref_exists(repo: Path, ref: str) -> bool:
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def missing_base_message(check: str, base: str) -> str:
    return (
        f"{check}: нет базы сравнения `{base}` (ADR-0030). Тег локальный: восстановить — "
        f"`git tag {BASE_TAG} 17b3f93` (в этом репозитории он стоит на базовом коммите шага 01). "
        "Проверка без базы не пропускается: тихий пропуск снял бы заморозку."
    )


def split_nul(output: str) -> list[str]:
    return [part for part in output.split("\0") if part]


def load_core_freeze_exceptions(
    exceptions_file: Path = EXCEPTIONS_FILE, repo: Path = REPO_ROOT
) -> dict[str, str]:
    """Читает `путь ADR-NNNN` по строке; пустые строки и `#` — комментарии.

    Исключение без номера ADR или с номером несуществующей записи — ошибка файла (FITNESS.md: «у
    каждого исключения — номер ADR»).
    """
    exceptions: dict[str, str] = {}
    problems: list[str] = []
    if not exceptions_file.is_file():
        return exceptions
    adr_dir = repo / ADR_DIR
    for number, raw in enumerate(exceptions_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _EXCEPTION_LINE.match(line)
        if parsed is None:
            problems.append(f"строка {number}: ожидается `путь ADR-NNNN`, а не `{line}`")
            continue
        adr = parsed["adr"]
        if not any(adr_dir.glob(f"{adr.removeprefix('ADR-')}-*.md")):
            problems.append(f"строка {number}: записи {adr} нет в {ADR_DIR}/")
            continue
        exceptions[parsed["path"]] = adr
    if problems:
        raise ValueError(f"{exceptions_file.name}: " + "; ".join(problems))
    return exceptions


def python_files(root: Path, under: str = "", skip: Iterable[str] = ()) -> Iterator[Path]:
    """`.py` под `root/under`; `skip` — префиксы путей от `root`, которые не обходятся."""
    skip_prefixes = tuple(s.rstrip("/") for s in skip)
    start = root / under if under else root
    for current, dirs, files in os.walk(start):
        here = Path(current)
        rel = here.relative_to(root).as_posix()
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in _SKIP_DIR_NAMES
            and not d.startswith(_SKIP_DIR_PREFIXES)
            and not _is_under((here / d).relative_to(root).as_posix(), skip_prefixes)
        )
        if _is_under(rel, skip_prefixes):
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield here / name


def _is_under(rel: str, prefixes: tuple[str, ...]) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in prefixes)


def absolute_imports(path: Path) -> list[tuple[str, int]]:
    """Абсолютные импорты файла как (точечное имя, строка).

    `from a.b import c` даёт `a.b.c`: по полному имени отличается модуль от имени в модуле.
    Файл, который не разбирается, импортировать нельзя — он пропускается, а не считается нарушением.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # чужие строки с `\s` — не наше дело
            tree = ast.parse(path.read_bytes())
    except (SyntaxError, ValueError):
        return []
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                name = node.module if alias.name == "*" else f"{node.module}.{alias.name}"
                found.append((name, node.lineno))
    return found


def top_level(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def make_repo(root: Path, files: Mapping[str, str], tag: str | None = BASE_TAG) -> Path:
    """Временный git-репозиторий с одним коммитом `files`; метка `tag` — на нём (тесты проверок)."""
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q")
    commit_files(root, files, "base")
    if tag:
        _run(root, "tag", tag)
    return root


def commit_files(root: Path, files: Mapping[str, str | None], message: str) -> None:
    """Записывает `files` (None — удалить) и коммитит."""
    write_files(root, files)
    _run(root, "add", "-A")
    _run(root, "commit", "-q", "-m", message)


def write_files(root: Path, files: Mapping[str, str | None]) -> None:
    for rel, text in files.items():
        target = root / rel
        if text is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def _run(root: Path, *args: str) -> None:
    config = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    config += ["-c", "core.autocrlf=false"]
    git(root, *config, *args)
