"""Print the rule files an editor must read for repository-relative source paths."""

import argparse
import fnmatch
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent


def _matches(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _matches(parts, pattern[1:]) or bool(parts and _matches(parts[1:], pattern))
    return bool(parts and fnmatch.fnmatchcase(parts[0], pattern[0]) and _matches(parts[1:], pattern[1:]))


def matching_rules(paths: Sequence[str], rules_dir: Path) -> list[Path]:
    """Resolve the union of path-scoped rules without opening the source files."""
    matched = []
    for rule in sorted(rules_dir.glob("*.md")):
        header = re.match(r'\A---\npaths:\n((?:[ \t]+- "[^"\n]+"\n)+)---\n', rule.read_text(encoding="utf-8"))
        if header is None:
            raise ValueError(f"invalid paths frontmatter: {rule}")
        patterns = re.findall(r'"([^"]+)"', header[1])
        if any(
            _matches(PurePosixPath(path).parts, tuple(pattern.split("/"))) for path in paths for pattern in patterns
        ):
            matched.append(rule)
    return matched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Repository-relative file paths; new files are accepted.")
    args = parser.parse_args()
    try:
        paths = [(REPO_ROOT / path).resolve().relative_to(REPO_ROOT).as_posix() for path in args.paths]
        rules = matching_rules(paths, REPO_ROOT / ".rules")
    except ValueError as error:
        parser.error(str(error))
    for rule in rules:
        print(rule.relative_to(REPO_ROOT).as_posix())


if __name__ == "__main__":
    main()
