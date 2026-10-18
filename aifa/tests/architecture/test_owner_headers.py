"""F12 (а) — заголовки владельца ставит один модуль службы (ADR-0014, CONTRACTS.md §2.1).

Текст имён `X-Ouroboros-*` встречается в коде службы только в клиенте ouroboros; любой другой
модуль, называющий их, — второе место, где можно выдать себя за владельца.
"""

from __future__ import annotations

import re
from pathlib import Path

from arch_support import REPO_ROOT, python_files

SERVICE_SRC = "aifa/finengine/src"
CLIENT = "aifa/finengine/src/finengine/assistant/ouro_client.py"
OWNER_HEADER = re.compile(r"x-ouroboros-(owner|chat|agent)-id", re.IGNORECASE)


def owner_headers_outside_the_client(root: Path) -> list[str]:
    problems = []
    for path in python_files(root, SERVICE_SRC):
        rel = path.relative_to(root).as_posix()
        if rel == CLIENT:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OWNER_HEADER.search(line):
                problems.append(
                    "F12 (ADR-0014): заголовок владельца ouroboros назван вне клиента — "
                    f"{rel}:{number}. Их ставит только {CLIENT}."
                )
    return problems


def test_owner_headers_are_named_only_by_the_ouroboros_client() -> None:
    """F12 (а): в коде службы заголовки владельца — только в `ouro_client`."""
    problems = owner_headers_outside_the_client(REPO_ROOT)
    assert not problems, "\n".join(problems)


def test_the_client_does_name_them() -> None:
    """Страховка F12: проверка видит клиента — иначе зелёная проверка ничего бы не доказывала."""
    text = (REPO_ROOT / CLIENT).read_text(encoding="utf-8")
    assert {name.lower() for name in OWNER_HEADER.findall(text)} >= {"owner", "chat"}


def test_f12_catches_a_second_place_that_sets_the_owner(tmp_path: Path) -> None:
    """F12: нарушитель — другой модуль службы с заголовком владельца (в любом регистре)."""
    target = tmp_path / "aifa/finengine/src/finengine/api/proxy.py"
    target.parent.mkdir(parents=True)
    target.write_text('headers = {"x-ouroboros-owner-id": uid}\n', encoding="utf-8")
    client = tmp_path / CLIENT
    client.parent.mkdir(parents=True)
    client.write_text('OWNER_HEADER = "X-Ouroboros-Owner-Id"\n', encoding="utf-8")

    problems = owner_headers_outside_the_client(tmp_path)

    assert len(problems) == 1 and "proxy.py:1" in problems[0] and "ADR-0014" in problems[0]
