"""Штатные файлы гейтвея — наши, а не стоковые (#187).

ЗАЧЕМ ЭТОТ ФАЙЛ. Ядро OpenClaw подмешивает в КАЖДЫЙ ход пять файлов из
воркспейса: `agents.md · soul.md · identity.md · user.md · tools.md`. С 21 июня
и до 09.08.2026 там лежал стоковый английский текст ядра — 12 312 байт про
«You're not a chatbot… Have opinions», «Fill this in during your first
conversation» и `BOOTSTRAP.md`. Он ехал в каждый диалог и задавал агенту чужую
рамку поведения. Никто не жаловался, потому что промах молчаливый: файлы есть,
ядро довольно, поведение объясняли чем угодно ещё.

Источник истины — `agent-workspace/bootstrap/`, доставка —
`deploy/sync_workspace_skills.sh`. Тексты согласованы в
`docs/INSTRUCTION_CONTRACT.md` §4.2, §4.3, §5.Г.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ:
  1. все пять файлов на месте и не пустые;
  2. влезают в лимиты ядра — иначе ядро молча урежет текст (`truncated`);
  3. в них не осталось стокового английского;
  4. в них нет внутренних имён — решение владельца о позиционировании:
     наружу агент только «AIFA, агент финансового анализа».

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вписать в любой из файлов слово «FA-CLAW» или
стоковую фразу — соответствующий тест обязан покраснеть.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ. Он читает копию В РЕПОЗИТОРИИ и ничего не
исполняет. Он не знает ни что файлы доехали до воркспейса гейтвея (доставка —
`deploy/sync_workspace_skills.sh`, и «недоставка» у нас болезнь номер один),
ни что ядро подмешало их в ход, ни что написанное ПОДЕЙСТВОВАЛО на поведение
агента. Зелёный здесь означает ровно одно: текст в ветке такой, как задумано.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "agent-workspace" / "bootstrap"

# Порядок и имена — из CONTEXT_FILE_ORDER ядра
# (/app/dist/system-prompt-config-CLAPATdy.js): agents:10, soul:20, identity:30,
# user:40, tools:50. Лимиты оттуда же.
FILES = ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md"]
LIMIT_PER_FILE = 20_000
LIMIT_TOTAL = 60_000
LIMIT_USER = 4_000

STOCK_MARKERS = [
    "You're not a chatbot",
    "Have opinions",
    "Fill this in",
    "pick something you like",
    "BOOTSTRAP.md",
]

# Наружу агент — «AIFA, агент финансового анализа». Внутренние имена продукта,
# гейтвея, движка и инструментов в конституцию не попадают: она едет в промпт
# целиком, и всё написанное там агент считает разрешённым к пересказу.
FORBIDDEN_NAMES = ["fa-claw", "claw", "ir-агент", "finengine", "openclaw",
                   "fe_analyze", "fe_extract", "workstation", "kimi", "glm"]


def _text(name: str) -> str:
    return (BOOTSTRAP / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_file_exists_and_is_not_empty(name):
    p = BOOTSTRAP / name
    assert p.is_file(), f"нет {p} — ядро возьмёт стоковый файл ядра"
    assert len(_text(name).strip()) >= 80, f"{name}: почти пусто, похоже на обрубок"


@pytest.mark.parametrize("name", FILES)
def test_fits_core_limit(name):
    n = len(_text(name))
    cap = LIMIT_USER if name == "USER.md" else LIMIT_PER_FILE
    assert n <= cap, f"{name}: {n} симв. при лимите {cap} — ядро урежет молча"


def test_all_files_together_fit_the_budget():
    total = sum(len(_text(n)) for n in FILES)
    assert total <= LIMIT_TOTAL, (
        f"суммарно {total} симв. при лимите {LIMIT_TOTAL} — часть текста не доедет"
    )


@pytest.mark.parametrize("name", FILES)
def test_no_stock_english_left(name):
    body = _text(name)
    for marker in STOCK_MARKERS:
        assert marker.lower() not in body.lower(), (
            f"{name}: остался стоковый текст ядра «{marker}»"
        )


@pytest.mark.parametrize("name", FILES)
def test_no_internal_names(name):
    body = _text(name).lower()
    hits = [w for w in FORBIDDEN_NAMES if w in body]
    assert not hits, (
        f"{name}: внутренние имена {hits} — решение владельца о позиционировании: "
        f"наружу только «AIFA, агент финансового анализа»"
    )


def test_identity_frontmatter_is_single_line():
    """Тот же парсер, что у навыков: многострочные ключи он не понимает."""
    lines = _text("IDENTITY.md").splitlines()
    assert lines[0].strip() == "---", "IDENTITY.md: нет frontmatter"
    end = next(i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---")
    for ln in lines[1:end]:
        assert ln and not ln.startswith((" ", "\t")), (
            f"IDENTITY.md: строка продолжения «{ln}» — парсер ядра поймёт только "
            f"однострочные ключи"
        )
        assert ":" in ln


@pytest.mark.parametrize("name", FILES)
def test_text_is_russian(name):
    """Дешёвый признак «файл подменили обратно на английский»."""
    letters = [c for c in _text(name) if c.isalpha()]
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё")
    assert letters and cyr / len(letters) >= 0.8, (
        f"{name}: кириллицы {cyr}/{len(letters)} — похоже на стоковый английский"
    )
