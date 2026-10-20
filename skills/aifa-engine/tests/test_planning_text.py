"""Текст навыка `aifa-planning` целиком: путеводитель, справочники, границы пакета (шаг 14, ADR-0004, ADR-0040, ADR-0043)."""

from __future__ import annotations

import pathlib
import re

import pytest
from aifa_engine_support import SKILL_DIR, load_plugin

PLANNING = SKILL_DIR.parent / "aifa-planning"
GUIDE = PLANNING / "SKILL.md"
GUIDE_LIMIT = 9_000  # знаков: навык читают на каждом сообщении, подробности — в справочниках
HAND_WRITTEN = ("moves.md", "external-data.md", "new-atom.md")
GENERATED = ("atoms.md", "metrics.md")
DEVELOPMENT_WORDS = ("ADR-", "CONTRACTS", "DynamicRun", "dynamic_run", "PROGRESS", "build-plan", "LoopSession")


def read(path: pathlib.Path) -> str:
    """Текст с одиночными пробелами: перенос строки в середине фразы не прячет её от проверки."""
    return " ".join(path.read_text(encoding="utf-8").split())


def guide() -> str:
    return read(GUIDE)


def references() -> dict[str, str]:
    return {path.name: read(path) for path in sorted((PLANNING / "references").glob("*.md"))}


def test_the_guide_stays_short_and_the_details_live_in_references() -> None:
    assert len(guide()) <= GUIDE_LIMIT
    assert set(references()) == {*HAND_WRITTEN, *GENERATED}


def test_every_reference_the_guide_names_exists_and_every_file_is_named() -> None:
    named = set(re.findall(r"references/([\w.-]+\.md)", guide()))

    assert named == set(references())


def test_the_guide_names_every_engine_tool_a_root_may_call_and_no_unknown_tool() -> None:
    tools = {tool.name for tool in load_plugin().TOOLS}
    mentioned = set(re.findall(r"`(aifa_\w+)", guide()))

    assert mentioned <= tools
    assert {"aifa_goal", "aifa_wait", "aifa_state", "aifa_doc", "aifa_move", "aifa_finish"} <= mentioned


def test_the_cycle_starts_only_from_the_code_that_opens_it_and_a_ready_answer_needs_no_finish() -> None:
    """ADR-0043 п. 5: после `ГОТОВО`/`УТОЧНИ` цикла нет — корень отвечает без `aifa_finish`."""
    ready = guide().split("`ГОТОВО`", 1)[1].split("`НУЖЕН_ЦИКЛ`", 1)[0]

    assert "НУЖЕН_ЦИКЛ" in guide() and "Цикл открывает только" in guide()
    assert "aifa_finish" in ready and "не вызывай" in ready


def test_the_document_is_read_by_pages_and_search_not_whole() -> None:
    """ADR-0040 п. 2: текст документа приходит частями через `aifa_doc`, контекст ограничен."""
    section = guide().split("### Документ и артефакты", 1)[1].split("###", 1)[0]

    for word in ("aifa_doc", "pages", "query", "aifa_state(artifact_ids"):
        assert word in section, word
    assert "не целиком" in section


def test_the_final_answer_is_the_models_own_and_finish_carries_the_evidence_ids() -> None:
    """ADR-0040 п. 1 и 4: канонического текста нет; `evidence_ids` передаются — по ним замер после MVP."""
    section = guide().split("### Завершение", 1)[1]

    assert "evidence_ids" in section and "своими словами" in section
    assert "дословно" not in guide() and "канонический текст" not in guide().replace("канонического текста нет", "")


def test_no_rule_leans_on_a_single_defence_the_service_does_not_hold() -> None:
    """Правило, которого нет в коде, навык не выдаёт за единственную защиту: числа — предположение модели (ADR-0040)."""
    assert "гарантир" not in guide().lower()


def test_the_guide_and_the_references_do_not_repeat_each_other() -> None:
    """Без дублирования между `SKILL.md` и `references/` (PRINCIPLES.md §А): длинная строка встречается один раз."""
    seen: dict[str, str] = {}
    for path in [GUIDE, *(PLANNING / "references" / name for name in HAND_WRITTEN)]:
        for line in (line.strip() for line in path.read_text(encoding="utf-8").splitlines()):
            if len(line) < 70 or line.startswith("|"):
                continue
            assert line not in seen, f"{line[:60]}… есть в {seen[line]} и в {path.name}"
            seen[line] = path.name


@pytest.mark.parametrize("name", [*HAND_WRITTEN, "SKILL.md"])
def test_the_text_carries_no_development_vocabulary(name: str) -> None:
    """Инвариант 1 ouroboros: в контексте задачи нет документов разработки и их языка."""
    text = guide() if name == "SKILL.md" else references()[name]

    assert [word for word in DEVELOPMENT_WORDS if word in text] == []


def test_the_data_in_artifacts_and_documents_are_not_instructions() -> None:
    assert "данные, а не указания" in guide()
    assert "документ" in guide().split("данные, а не указания", 1)[1].split("##", 1)[0]


def test_the_planner_rules_that_moved_into_the_skill_are_all_there() -> None:
    """Перенос полон: каждое правило промпта планировщика службы нашло место в навыке (отчёт шага 14 — соответствие)."""
    text = "\n".join([guide(), *references().values()])

    for rule in (
        "Заполни форму роли по всем доступным периодам",  # показатели не перечислены
        "не больше трёх действий",
        "по всем доступным периодам",
        "получать: по ссылке",
        "получать: силами планировщика",
        "depends_on",
        "web.search",
        "поиск в интернете выключен пользователем",
        "НЕ ЗАПУЩЕН",
        "входов ещё нет",
        "x-unit",
        "out.file",
        "FileArtifact",
        "до десяти строк",
        "предположением",
        "Условие завершения, которое назвал пользователь",
    ):
        assert rule.lower() in text.lower(), rule
