"""Разбор потока модели на блоки по показателям — по мере прихода.

Модель отдаёт JSON кусками; ждать конца ответа значит не показывать
пользователю ничего минутами. Этот итератор выхватывает завершённый объект
показателя, как только он дописан, и отдаёт дальше.
"""
from __future__ import annotations



def _metric_block_iter(buffer: str, valid_enums: set[str]):
    """Yield (enum, json_object_text) for complete metric blocks in a JSON stream.

    Unlike the old regex, this handles nested objects such as
    {"values": {"Year ended January 31, 2024": "..."}} and arrays. It is intentionally tolerant:
    it scans for `"ENUM": {` and then waits until braces balance outside
    strings.
    """
    import re as _re
    for m in _re.finditer(r'"([A-Z][A-Z0-9_]+)"\s*:\s*\{', buffer):
        enum = m.group(1)
        if enum not in valid_enums:
            continue
        start = buffer.find("{", m.end() - 1)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(buffer)):
            ch = buffer[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield enum, buffer[start:i + 1]
                    break
