"""F7 — фронтенд говорит только со службой (ADR-0014, ADR-0028, CONTRACTS.md §1).

Браузер не ходит в ouroboros: ни по WebSocket хода, ни по его HTTP (`/api/chat/…`, `/api/files`,
`/api/agents`), ни с заголовками владельца `X-Ouroboros-*`. Исходники `aifa/web/src` читаются как
текст.

Оговорка к тексту FITNESS.md, которая вошла вместе с ADR-0047: префикс `/api` в браузере — это
прокси
службы (`next.config.ts`), поэтому `/api/chat/history/<сессия>` — ручка службы `GET /chat/history/…`
(C1), а не `GET /api/chat/history` ouroboros. Запрещены пути ouroboros, а не любой путь под
`/api/chat/`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from arch_support import REPO_ROOT

WEB_SRC = "aifa/web/src"
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs")
SKIP_DIRS = frozenset({"node_modules", ".next", "__pycache__"})
# (шаблон, что это в ouroboros). Каждый шаблон ловит путь ouroboros и не ловит одноимённую ручку
# службы.
FORBIDDEN = (
    (re.compile(r"""["'`]/ws(?:["'`?/])"""), "WebSocket хода ouroboros (`/ws`)"),
    (re.compile(r"new\s+WebSocket\("), "WebSocket из браузера (ход идёт по SSE службы)"),
    (re.compile(r"/api/chat/(?!history)"), "HTTP чата ouroboros (`/api/chat/…`)"),
    (re.compile(r"/api/chat/history(?![/\w])"), "история чата ouroboros (`/api/chat/history`)"),
    (re.compile(r"/api/files"), "каталог файлов ouroboros (`/api/files`)"),
    (re.compile(r"/api/agents"), "агенты ouroboros (`/api/agents`)"),
    (re.compile(r"x-ouroboros-", re.IGNORECASE), "заголовки владельца ouroboros (`X-Ouroboros-*`)"),
)


def web_sources(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(root / WEB_SRC):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        found.extend(
            Path(current) / name for name in sorted(files) if name.endswith(SOURCE_SUFFIXES)
        )
    return found


def frontend_calls_ouroboros(root: Path) -> list[str]:
    problems = []
    for path in web_sources(root):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern, what in FORBIDDEN:
                if pattern.search(line):
                    problems.append(
                        f"F7 (ADR-0014, ADR-0028): фронтенд называет {what} — "
                        f"{path.relative_to(root).as_posix()}:{number}. "
                        "Браузер говорит только со службой."
                    )
    return problems


def _write(root: Path, rel: str, text: str) -> None:
    target = root / WEB_SRC / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_the_frontend_talks_only_to_the_service() -> None:
    """F7: в исходниках фронтенда нет путей и заголовков ouroboros."""
    problems = frontend_calls_ouroboros(REPO_ROOT)
    assert not problems, "\n".join(problems)


def test_the_check_sees_the_frontend_it_guards() -> None:
    """Страховка F7: пустое множество файлов дало бы зелёную проверку, которая ничего не
    доказывает."""
    assert len(web_sources(REPO_ROOT)) > 50


def test_the_history_of_the_service_is_not_the_history_of_ouroboros(tmp_path: Path) -> None:
    """F7: `/api/chat/history/<сессия>` — ручка службы за прокси (C1); без сессии это путь
    ouroboros."""
    _write(tmp_path, "lib/ok.ts", "fetch(`/api/chat/history/${encodeURIComponent(id)}`);\n")
    _write(tmp_path, "lib/bad.ts", 'fetch("/api/chat/history?limit=5");\n')

    problems = frontend_calls_ouroboros(tmp_path)

    assert len(problems) == 1 and "bad.ts:1" in problems[0] and "ADR-0014" in problems[0]


def test_f7_catches_every_way_of_reaching_ouroboros(tmp_path: Path) -> None:
    """F7: нарушитель на каждый запрещённый вид обращения."""
    _write(tmp_path, "a.ts", 'new WebSocket("ws://host/ws");\nconst s = "/ws";\n')
    _write(tmp_path, "b.tsx", 'fetch("/api/chat/upload", { method: "POST" });\n')
    _write(tmp_path, "c.ts", "fetch(`/api/files/${id}/content`);\n")
    _write(tmp_path, "d.ts", 'fetch("/api/agents");\n')
    _write(tmp_path, "e.ts", 'headers: { "X-Ouroboros-Owner-Id": id },\n')

    problems = frontend_calls_ouroboros(tmp_path)

    flagged = sorted({p.split("src/")[1].split(":")[0] for p in problems})
    assert flagged == ["a.ts", "b.tsx", "c.ts", "d.ts", "e.ts"]


def test_f7_does_not_confuse_the_service_routes_of_the_proxy_with_ouroboros(tmp_path: Path) -> None:
    """F7: `/chat/turns`, `/dialogs`, `/documents/…` через `/api` — службы."""
    _write(
        tmp_path,
        "lib/api.ts",
        'const BASE = "/api"; fetch(`${BASE}/chat/turns`); fetch("/api/dialogs");\n',
    )
    _write(tmp_path, "lib/words.ts", 'const msg = "wsdl"; const p = "/wsgi";\n')

    assert frontend_calls_ouroboros(tmp_path) == []
