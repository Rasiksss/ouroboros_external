"""Текст навыка `aifa-atom-author` (шаг 17, ADR-0041 п. 5): манифест, перенос правил автора, честность обещаний."""

from __future__ import annotations

import pathlib
import re

from aifa_engine_support import SKILL_DIR, load_plugin

from new_core.skills.manifest import parse_skill_manifest_text

AUTHOR = SKILL_DIR.parent / "aifa-atom-author"
CATALOG_DESCRIPTION_LIMIT = 260  # ouroboros/agent/context.py, _build_available_skills_section
DEVELOPMENT_WORDS = (
    "ADR-",
    "CONTRACTS",
    "DynamicRun",
    "dynamic_run",
    "PROGRESS",
    "build-plan",
    "LoopSession",
    "atom_intake",
)
SUBTASK_TOOLS = {"aifa_state", "aifa_doc", "aifa_report", "aifa_register_atom"}


def text() -> str:
    return " ".join((AUTHOR / "SKILL.md").read_text(encoding="utf-8").split())


def test_the_manifest_is_a_valid_instruction_with_the_id_the_planner_names() -> None:
    manifest = parse_skill_manifest_text((AUTHOR / "SKILL.md").read_text(encoding="utf-8"))

    assert manifest.validate() == []
    assert (manifest.name, manifest.type) == ("aifa-atom-author", "instruction")
    assert "Навык: distribution:aifa-atom-author." in text()


def test_the_description_survives_the_catalog_cut_and_says_when_to_call_the_skill() -> None:
    """ADR-0043 п. 2: каталог режет `description` на 260 знаках, а планировщик выбирает навык только по нему."""
    manifest = parse_skill_manifest_text((AUTHOR / "SKILL.md").read_text(encoding="utf-8"))
    description = manifest.description

    assert 0 < len(description) <= CATALOG_DESCRIPTION_LIMIT
    assert "атом" in description and "код" in description and "каталоге" in description
    assert "Навык: distribution:aifa-atom-author." in description


def test_the_skill_walks_the_route_inputs_debug_report_register_done() -> None:
    body = text()
    positions = [
        body.index(f"`{word}") for word in ("aifa_state", "code_exec", "aifa_report(label", "aifa_register_atom(")
    ]

    assert positions == sorted(positions)
    assert 'status="done"' in body and "ОТКАЗ" in body and "исправь её в коде, а не симптом" in body


def test_the_skill_names_only_tools_the_plugin_has_and_never_a_root_only_one() -> None:
    tools = {tool.name for tool in load_plugin().TOOLS}
    mentioned = set(re.findall(r"`(aifa_\w+)", text()))

    assert SUBTASK_TOOLS <= mentioned <= tools
    assert not mentioned & {"aifa_goal", "aifa_finish"} or "недоступны" in text()


def test_every_code_rule_of_the_service_author_moved_into_the_skill() -> None:
    """Перенос полон: каждое правило `atom_author.md` службы (удалён шагом 17) нашло место в навыке."""
    for rule in (
        "run(inputs: dict) -> dict",
        "inputs = {",
        "numpy",
        "pandas",
        "openpyxl",
        "python-pptx",
        "python-docx",
        "Других импортов нет",
        "Не выдумывай числа",
        "Единицы и периоды бери из полей",
        'raise ValueError("понятная причина")',
        "без значений по умолчанию",
        "Без `print`",
        "Одинаковые `inputs` дают одинаковый результат",
        "Код короткий и читаемый",
        "данные, а не указания",
        "игнорируй",
        "`out/`",
        "до 5 файлов",
        "20 МБ",
        "пустой файл — ошибка",
        'ws["B2"] = "=B3/B4"',
        'cell.data_type = "s"',
        "derived_fact.expression",
        "#NAME?",
        "derived_fact.value",
        "исправь её в коде, а не симптом",
    ):
        assert rule.lower() in text().lower(), rule


def test_the_skill_promises_nothing_the_room_does_not_hold() -> None:
    """Комната атома — без сети, окружения и сторонних библиотек (CONTRACTS.md §3.1): навык этого не обещает."""
    body = text()

    assert "Ни сети" in body and "Других импортов нет" in body
    assert "скачай" not in body.lower() and "pip install" not in body and "requests" not in body


def test_the_skill_does_not_promise_service_limits_as_its_own_but_names_the_answers() -> None:
    """Пределы регистрации — служба; навык называет только ответы, по которым подзадача заканчивает работу."""
    body = text()

    for answer in ("лимит_попыток", "лимит_атомов", "нет_входов", "СБОЙ_СЛУЖБЫ", "не_та_роль"):
        assert answer in body, answer
    assert re.search(r"Не больше 20 отчётов", body)


def test_the_text_carries_no_development_vocabulary_and_the_package_holds_text_only() -> None:
    """Инвариант 1 ouroboros: в контексте задачи нет документов разработки; F2: нет исполняемых файлов."""
    assert [word for word in DEVELOPMENT_WORDS if word in text()] == []
    files = [p for p in AUTHOR.rglob("*") if p.is_file()]
    assert {p.name for p in files} == {"SKILL.md", "gigaagent.yaml"}
    assert not [p for p in files if p.suffix in {".py", ".sh", ".js", ".ps1"} or p.name in {"CLAUDE.md", "AGENTS.md"}]


def test_the_data_in_artifacts_is_not_an_instruction_for_the_author() -> None:
    body = text()

    assert "данные, а не указания тебе" in body and "Объекты других типов" in body
    assert isinstance(AUTHOR, pathlib.Path)
