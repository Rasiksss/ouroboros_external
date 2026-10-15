"""Одно пояснение — один раз: копия комментария висит над ЧУЖИМ кодом.

ЦЕНА (Б5, 20.08.2026). В `api/main.py` пояснение к полю `inputs` было вставлено
ДВАЖДЫ: первая копия объясняла своё поле, вторая осталась сиротой и стояла над
блоком про текстовые поля — то есть описывала не то, что под ней. Правило
`docs/rules/coding_standards.md` требует от комментария объяснять следующий за
ним код; копия это правило нарушает молча и переживает любое ревью по диффу,
потому что обе копии выглядят осмысленно поодиночке.

ЧТО СЧИТАЕМ. Блок — подряд идущие строки-комментарии. Прагмы (`noqa`, `type:`,
`ruff:`) повторяются законно и в счёт не идут: это указания инструментам, а не
объяснение кода. Однострочные блоки тоже не считаем — «# ── Шапка ──» и прочие
разделители повторяются по делу.

ПРОВЕРКА СТОРОЖА ОТКАТОМ (проделана 20.08): вернуть в `api/main.py` вторую
копию пояснения к `inputs` — тест краснеет и называет файл и строку.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ (ревизия 21.08.2026). Это ЛИНТЕР на комментарии, а
не проверка поведения: он ловит ровно ДОСЛОВНУЮ копию блока в одном файле. Мимо
проходит всё остальное, включая ту самую болезнь в её обычном виде — одиночный
комментарий, объясняющий не тот код, над которым стоит; копия, разошедшаяся на
одно слово; копия, разложенная по ДВУМ файлам; устаревшее пояснение, которое
описывает снесённый механизм. Ни одна правка кода, меняющая поведение продукта,
покраснеть его не заставит.
"""
from __future__ import annotations

import io
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "finengine"

_PRAGMAS = ("# noqa", "# type:", "# ruff:", "# pragma", "# fmt:")


def _comment_blocks(path: Path) -> list[tuple[int, str]]:
    """Блоки подряд идущих комментариев: (строка начала, текст блока)."""
    blocks: list[tuple[int, str]] = []
    lines: list[str] = []
    start = 0
    previous = -2
    tokens = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        text = token.string.strip()
        if text.startswith(_PRAGMAS):
            continue
        if token.start[0] != previous + 1:
            if len(lines) > 1:
                blocks.append((start, "\n".join(lines)))
            lines = []
            start = token.start[0]
        lines.append(text)
        previous = token.start[0]
    if len(lines) > 1:
        blocks.append((start, "\n".join(lines)))
    return blocks


def test_no_comment_block_is_pasted_twice_in_one_file():
    files = sorted(SRC.rglob("*.py"))
    assert files, "исходников не найдено — тест ослеп"
    duplicates: list[str] = []
    for path in files:
        seen: dict[str, int] = {}
        for line, block in _comment_blocks(path):
            if block in seen:
                duplicates.append(
                    f"{path.relative_to(SRC)}: строки {seen[block]} и {line} — "
                    f"одно пояснение дважды: {block.splitlines()[0]}"
                )
                continue
            seen[block] = line
    assert not duplicates, "копия комментария объясняет не тот код, над которым стоит:\n" + "\n".join(duplicates)
