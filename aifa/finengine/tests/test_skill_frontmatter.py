"""Frontmatter навыка обязан парситься ядром — иначе навык молча полу-мёртв.

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. Парсер frontmatter в OpenClaw поддерживает
ТОЛЬКО ОДНОСТРОЧНЫЕ ключи (`/app/docs/tools/skills.md`, раздел «SKILL.md
format»: «The frontmatter parser supports **single-line keys only** —
`metadata` must be a single-line JSON object»). Все наши SKILL.md писались с
многострочным YAML:

    metadata:
      openclaw:
        emoji: "🧮"

Пока механизм навыков был погашен (`agents.defaults.skills: []`), это ничего
не ломало — файлы никто не читал. С включением механизма такой блок молча не
распарсится, и узнать об этом было бы неоткуда: навык бы «был», просто без
метаданных, а то и целиком мимо.

Здесь же проверяется `description` — на нём ядро РЕШАЕТ, брать навык в работу.
Пустое или отсутствующее описание = навык лежит и не выбирается никогда; это
тот же молчаливый отказ.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в любой SKILL.md многострочный `metadata:` —
тест обязан покраснеть.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ И ПОЧЕМУ ОН ОСТАЁТСЯ ТЕКСТОВЫМ (дописано
ревизией 21.08.2026). Frontmatter разбирает ЯДРО OPENCLAW, и его разборщика в
нашем репозитории нет — ни на питоне, ни на TypeScript: исполнить тут нечего,
проверять можно только форму. Форма списана с чужой документации
(`/app/docs/tools/skills.md`), то есть сторож повторяет ПРАВИЛО, прочитанное
однажды. Ядро поменяет разборщик — сторож этого не заметит и продолжит
уверенно зеленеть на файлах, которые ядро больше не понимает. И главное: он
проверяет, что ядро СМОЖЕТ прочитать описание, а не что модель ВЫБЕРЕТ по
нему навык. Порог в 40 символов — цифра из головы, а не измеренная граница.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "station-skills").glob("*/SKILL.md"))


def _frontmatter(path: Path) -> list[str]:
    """Строки между первой и второй строкой '---'."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "---", f"{path}: нет frontmatter в первой строке"
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    assert end is not None, f"{path}: frontmatter не закрыт"
    return lines[1:end]


def test_skills_found() -> None:
    """Пустой список молча сделал бы все проверки ниже зелёными."""
    assert SKILLS, "не найдено ни одного station-skills/*/SKILL.md — тест устарел"


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_frontmatter_keys_are_single_line(path: Path) -> None:
    for line in _frontmatter(path):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        assert not line.startswith((" ", "\t")), (
            f"{path}: строка продолжения «{line.strip()}» — парсер ядра понимает "
            f"только ОДНОСТРОЧНЫЕ ключи. metadata пиши однострочным JSON: "
            f'metadata: {{"openclaw": {{"emoji": "…"}}}}'
        )
        assert ":" in line, f"{path}: строка без ключа: «{line}»"


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_metadata_is_valid_single_line_json(path: Path) -> None:
    for line in _frontmatter(path):
        key, _, value = line.partition(":")
        if key.strip() != "metadata":
            continue
        try:
            parsed = json.loads(value.strip())
        except json.JSONDecodeError as exc:
            pytest.fail(f"{path}: metadata не разбирается как JSON — {exc}")
        assert isinstance(parsed, dict), f"{path}: metadata обязан быть объектом"


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_name_matches_directory(path: Path) -> None:
    """Имя во frontmatter = имя каталога, иначе ядро не сопоставит навык."""
    names = [ln.partition(":")[2].strip() for ln in _frontmatter(path)
             if ln.partition(":")[0].strip() == "name"]
    assert names, f"{path}: нет обязательного ключа name"
    assert names[0] == path.parent.name, (
        f"{path}: name={names[0]!r}, а каталог {path.parent.name!r}"
    )


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_description_is_present_and_useful(path: Path) -> None:
    """По description ядро решает, брать ли навык. Пустое = навык не выберут."""
    descs = [ln.partition(":")[2].strip() for ln in _frontmatter(path)
             if ln.partition(":")[0].strip() == "description"]
    assert descs, f"{path}: нет обязательного ключа description"
    assert len(descs[0]) >= 40, (
        f"{path}: description длиной {len(descs[0])} — по нему модель выбирает "
        f"навык среди прочих, короткое описание = навык не выберут никогда"
    )
