"""Текст HTML-страницы без разметки — один код для `fetch_url` свободного чата и атома-загрузки цикла.

Намеренно дёшево: блоки `<script>`/`<style>` убираются целиком, остальные теги — по одному, пробелы
схлопываются. Страница, которой нужен JavaScript, так не читается — это известная граница.

Кодировка — та, что объявлена: заголовком ответа, у страницы — ещё и `<meta charset>`; не объявлена —
UTF-8. То, что в ней не читается, текстом не становится: `decode_text` отдаёт `None`."""
from __future__ import annotations

import re

_SCRIPT_OR_STYLE = re.compile(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w-]+)", re.I)
_META_SEARCH_BYTES = 2048
_UNREADABLE_FEW = 16
_UNREADABLE_SHARE = 0.05


def html_to_text(html: str) -> str:
    return _SPACE.sub(" ", _TAG.sub(" ", _SCRIPT_OR_STYLE.sub(" ", html))).strip()


def is_html(content_type: str) -> bool:
    return content_type.split(";")[0].strip().lower() in _HTML_CONTENT_TYPES


def meta_charset(content: bytes) -> str:
    found = _META_CHARSET.search(content[:_META_SEARCH_BYTES])
    return found.group(1).decode("ascii") if found else ""


def decode_text(content: bytes, charset: str) -> str | None:
    """Единичные нечитаемые байты заменяются знаком замены — файл данных с парой битых байтов остаётся
    файлом данных. `None` — это не текст: есть нулевой байт либо нечитаемого больше `_UNREADABLE_FEW` знаков и
    больше `_UNREADABLE_SHARE` текста
    (PDF, архив, кириллица в необъявленной однобайтовой кодировке)."""
    try:
        text = content.decode(charset or "utf-8", errors="replace")
    except LookupError:
        text = content.decode("utf-8", errors="replace")
    return None if "\x00" in text or text.count("\ufffd") > max(_UNREADABLE_FEW, len(text) * _UNREADABLE_SHARE) else text


__all__ = ["decode_text", "html_to_text", "is_html", "meta_charset"]
