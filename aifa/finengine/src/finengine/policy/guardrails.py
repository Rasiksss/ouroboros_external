"""Output guardrails — backend-side scrubber for SSE events.

Defense-in-depth on top of POLICY.md + SAFETY block injection. If the
model ignores its rules and tries to leak API keys, internal paths, or
model identity, this layer scrubs the leak before it leaves the
backend.

Two API surfaces:
  - `scrub(text) -> text`        for one-shot scrubs (tool outputs)
  - `StreamScrubber`             for SSE text_delta streams; buffers
    a sliding window so patterns spanning a chunk boundary still match

PATTERNS list is the canonical denylist. Keep it conservative — false
positives (over-redaction) are better than false negatives (a leaked
key in the wild). All replacements are `[REDACTED:<reason>]` so the
user can tell something was scrubbed without seeing the value.
"""
from __future__ import annotations

import logging

import re
from typing import Iterable

# Each entry: (compiled regex, replacement, short reason for [REDACTED:reason]).
# Order matters — earlier rules win when they overlap.
PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # ── API keys / tokens ────────────────────────────────────────────
    (re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{20,}"), "[REDACTED:OPENROUTER_KEY]", "OPENROUTER_KEY"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED:ANTHROPIC_KEY]", "ANTHROPIC_KEY"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "[REDACTED:OPENAI_KEY]", "OPENAI_KEY"),
    (re.compile(r"\bsk-[A-Za-z0-9]{30,}\b"), "[REDACTED:API_KEY]", "API_KEY"),
    (re.compile(r"tvly-(?:dev-)?[A-Za-z0-9_\-]{15,}"), "[REDACTED:TAVILY_KEY]", "TAVILY_KEY"),
    # Generic JWT-like (header.payload.signature, all base64url)
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED:JWT]", "JWT"),
    # Bearer headers in raw text
    (re.compile(r"(?i)Authorization:\s*Bearer\s+\S+"), "Authorization: Bearer [REDACTED:BEARER]", "BEARER"),
    # ── Internal paths / IPs ─────────────────────────────────────────
    (re.compile(r"/root/\.openclaw/[\w./\-]*"), "[REDACTED:INTERNAL_PATH]", "INTERNAL_PATH"),
    (re.compile(r"/opt/finagent/(?:deploy/)?\.env\S*"), "[REDACTED:INTERNAL_ENV]", "INTERNAL_ENV"),
    (re.compile(r"/opt/fa-claw-agent/[\w./\-]*"), "[REDACTED:INTERNAL_PATH]", "INTERNAL_PATH"),
    # Internal IPs (loopback + docker bridges)
    (re.compile(r"\b127\.0\.0\.1:\d{2,5}\b"), "[REDACTED:LOCALHOST]", "LOCALHOST"),
    (re.compile(r"\b172\.1[78]\.0\.\d{1,3}(?::\d+)?\b"), "[REDACTED:DOCKER_IP]", "DOCKER_IP"),
    (re.compile(r"\b85\.198\.84\.50\b"), "[REDACTED:HOST_IP]", "HOST_IP"),
    # ── Model / provider identity ────────────────────────────────────
    # We want the agent to identify as FA-CLAW, never as the underlying
    # model. Catch direct names + provider/model slugs.
    (re.compile(r"(?i)moonshotai/kimi[\w.\-]*"), "[REDACTED:MODEL]", "MODEL"),
    (re.compile(r"(?i)\bkimi[ _\-]?k\d(?:\.\d+)?\b"), "[REDACTED:MODEL]", "MODEL"),
    (re.compile(r"(?i)\bopenrouter\.ai/\S+"), "[REDACTED:PROVIDER_URL]", "PROVIDER_URL"),
    (re.compile(r"(?i)\b(?:moonshot|deepseek|qwen|glm-?\d+|claude-?\d+(?:\.\d+)?|gpt-?\d+(?:\.\d+)?|llama[\-\w]*)\b"), "[REDACTED:MODEL]", "MODEL"),
]


def scrub(text: str) -> str:
    """One-shot scrub. Use for tool outputs, error messages, anything
    not streamed in chunks."""
    out = text
    for pat, repl, _ in PATTERNS:
        out = pat.sub(repl, out)
    return out


# Longest pattern length we care about (rough upper bound) — keys
# can be 60+ chars. We hold back this many chars at the tail of the
# stream buffer so any pattern spanning a delta boundary still
# matches before we emit.
log = logging.getLogger(__name__)

_TAIL_HOLDBACK = 100

# Пороги обнаружения зацикливания модели (17.08.2026, см. `_degeneration_cut`).
# Буквы и цифры подряд в осмысленном тексте не идут — порог низкий. Дефисы,
# равно, решётки, пробелы законно идут рядами (разделители таблиц, линии,
# выравнивание) — им порог заведомо выше любой разумной разметки.
_RUN_LIMIT_ALNUM = 30
_RUN_LIMIT_OTHER = 120
_DEGENERATE_NOTE = "\n\n_⚠️ Ответ оборван: модель зациклилась на повторе. Переспросите._"


class StreamScrubber:
    """Stateful scrubber for SSE text_delta streams.

    The LLM emits text in chunks (5-50 tokens each). A 60-char API key
    may straddle two chunks: `"sk-or-v1-"` + `"abc...49c"`. Per-chunk
    regex misses it. So we keep a rolling buffer of the last
    `_TAIL_HOLDBACK` chars, emit only what's safely past that tail,
    and flush the remainder at end-of-stream.

    Usage:
        sc = StreamScrubber()
        for chunk in upstream_deltas:
            emit = sc.feed(chunk)
            if emit:
                yield {"type": "text_delta", "text": emit}
        final = sc.flush()
        if final:
            yield {"type": "text_delta", "text": final}
    """

    def __init__(self, tail_holdback: int = _TAIL_HOLDBACK):
        self._buf = ""
        self._tail = tail_holdback
        # Обнаружение ЗАЦИКЛИВАНИЯ модели (17.08.2026). Состояние тянется
        # между кусками: повтор приходит порциями по 5–50 знаков, и в
        # отдельном куске он не виден.
        self._run_char = ""
        self._run_len = 0
        self._degenerate = False

    def _degeneration_cut(self, text: str) -> tuple[str, bool]:
        """Обрезать текст в месте, где модель сорвалась в повтор одного знака.

        ЖИВОЙ СЛУЧАЙ 17.08 НА ПРОДЕ. Ответ оборвался фразой «в собранном
        дайджесте за по» и дальше шло 394 подряд «П». Пользователь увидел
        экран мусора. По истории диалогов это третий случай: 10.06 были
        пробелы ×28 и ×65, все три — на `qwen/qwen3.6-plus`. То есть модель
        срывается сама, и защищаться надо на нашей стороне.

        ДВА ПОРОГА, А НЕ ОДИН. Буквы и цифры подряд не повторяются в осмысленном
        тексте никогда — там порог низкий. А вот дефисы, равно, решётки и
        пробелы законно идут длинными рядами: разделители таблиц, горизонтальные
        линии, выравнивание. Один общий порог либо пропускал бы вырождение, либо
        рубил бы нормальную разметку.
        """
        for i, ch in enumerate(text):
            if ch == self._run_char:
                self._run_len += 1
            else:
                self._run_char, self._run_len = ch, 1
            limit = _RUN_LIMIT_ALNUM if ch.isalnum() else _RUN_LIMIT_OTHER
            if self._run_len >= limit:
                # Отрезаем ВЕСЬ повтор, а не только хвост за порогом.
                keep = text[: i + 1 - self._run_len]
                log.warning(
                    "поток оборван: модель зациклилась на %r (подряд %d)",
                    ch, self._run_len,
                )
                return keep, True
        return text, False

    def feed(self, text: str) -> str:
        """Add new text, return cleaned text safe to emit immediately.

        Режем по ПРОБЕЛУ, а не по фиксированным `_tail` символам. Секрет —
        одно «слово»: ни ключ, ни токен, ни путь не содержат пробела, значит
        разрыв на пробеле не может рассечь совпадение, и придерживать целую
        сотню символов незачем. Раньше именно из-за этого ответ выползал
        рывками по ~100 знаков и выглядел заторможенным, хотя модель писала
        ровно. Придерживаем только незаконченное последнее слово.

        Страховка от текста без пробелов (длинная строка таблицы, base64):
        если хвост перевалил за `_tail`, отдаём его насильно — иначе такой
        кусок висел бы в буфере до конца потока.
        """
        if not text:
            return ""
        if self._degenerate:
            # Один раз сказали — дальше поток молчит: досыпать мусор незачем.
            return ""
        text, hit = self._degeneration_cut(text)
        if hit:
            self._degenerate = True
            self._buf += text
            out = scrub(self._buf) + _DEGENERATE_NOTE
            self._buf = ""
            return out
        self._buf += text
        cut = max(self._buf.rfind(" "), self._buf.rfind("\n"), self._buf.rfind("\t"))
        if cut < 0:
            # Ни одного пробела: держим, пока не упрёмся в потолок.
            if len(self._buf) <= self._tail:
                return ""
            head, self._buf = self._buf[: -self._tail], self._buf[-self._tail :]
            return scrub(head)
        head, self._buf = self._buf[: cut + 1], self._buf[cut + 1 :]
        return scrub(head)

    def flush(self) -> str:
        """Return all buffered + scrubbed text. Call at end-of-stream."""
        out = scrub(self._buf)
        self._buf = ""
        return out


def scrub_dict(payload: dict, fields: Iterable[str] = ("text", "output", "summary", "message")) -> dict:
    """Helper: scrub specific string fields in a dict (e.g. SSE event)."""
    cleaned = dict(payload)
    for k in fields:
        if k in cleaned and isinstance(cleaned[k], str):
            cleaned[k] = scrub(cleaned[k])
    return cleaned


__all__ = ["scrub", "scrub_dict", "StreamScrubber", "PATTERNS"]
