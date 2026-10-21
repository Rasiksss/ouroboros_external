"""Манифесты `aifa-engine` и `aifa-planning`; тесты инструкции здесь — в её пакете только текст."""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from new_core.skills.manifest import parse_skill_manifest_text

SKILLS = pathlib.Path(__file__).resolve().parents[2]
ENGINE = SKILLS / "aifa-engine"
PLANNING = SKILLS / "aifa-planning"
CATALOG_DESCRIPTION_LIMIT = 260  # ouroboros/agent/context.py, _build_available_skills_section
DEVELOPMENT_DOCS = ("CLAUDE.md", "AGENTS.md")


def planning_text() -> str:
    """SKILL.md и все справочники пакета: правила навыка читаются по всем его файлам."""
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(PLANNING.rglob("*.md")))


def manifest_of(skill: pathlib.Path):
    return parse_skill_manifest_text((skill / "SKILL.md").read_text(encoding="utf-8"))


def test_the_engine_manifest_is_a_valid_extension_that_loads_plugin_py() -> None:
    manifest = manifest_of(ENGINE)

    assert manifest.validate() == []
    assert (manifest.name, manifest.type, manifest.entry) == ("aifa-engine", "extension", "plugin.py")
    assert (ENGINE / manifest.entry).is_file()


def test_the_engine_asks_only_for_the_tool_and_network_capabilities() -> None:
    """P2 (R20): адрес и токен — `os.environ`, не `read_settings`; других способностей навыку не нужно."""
    manifest = manifest_of(ENGINE)

    assert sorted(manifest.permissions) == ["net", "tool"]
    assert manifest.env_from_settings == []


def test_the_engine_description_survives_the_catalog_cut() -> None:
    assert 0 < len(manifest_of(ENGINE).description) <= CATALOG_DESCRIPTION_LIMIT


def test_the_planning_manifest_is_a_valid_instruction() -> None:
    manifest = manifest_of(PLANNING)

    assert manifest.validate() == []
    assert (manifest.name, manifest.type) == ("aifa-planning", "instruction")


def test_the_planning_description_survives_the_catalog_cut_and_names_the_subtask_exception() -> None:
    """ADR-0043 п. 2: каталог обрезает `description` на 260 знаках, а признак подзадачи должен остаться видимым."""
    description = manifest_of(PLANNING).description

    assert 0 < len(description) <= CATALOG_DESCRIPTION_LIMIT
    assert "Навык: <id>." in description
    assert re.search(r"кажд\w+ сообщени", description), "корень читает навык на каждом сообщении"


def test_the_first_rule_of_planning_lets_a_subtask_skip_it() -> None:
    """ADR-0043 п. 2: первое правило — подзадача («Навык: <id>.») читает названный навык, этот не применяет."""
    body = manifest_of(PLANNING).body
    first_rule = body.split("1.", 1)[1].split("2.", 1)[0]

    assert "Навык: <id>." in first_rule and "подзадача" in first_rule


def test_planning_names_both_tools_and_every_code_the_goal_can_answer_with() -> None:
    body = manifest_of(PLANNING).body

    for word in ("aifa_goal", "aifa_wait", "ГОТОВО", "ЖДИ", "УТОЧНИ", "ОТКАЗ", "СБОЙ_СЛУЖБЫ", "нет_документа"):
        assert word in body, word


def test_the_root_never_writes_or_registers_code_it_delegates_it() -> None:
    """ADR-0041, ADR-0043 п. 6: код атома корень не пишет и не сдаёт — `aifa_register_atom` и `aifa_report` только у подзадачи."""
    guide = manifest_of(PLANNING).body

    for word in ("aifa_register_atom", "aifa_report"):
        assert f"`{word}" not in guide, word
    assert "в `new_atoms` не присылай" in guide


def test_the_delegation_section_tells_the_whole_route_of_a_subtask() -> None:
    """Шаг 17, ADR-0041 п. 1–3, ADR-0043 пп. 3, 8: выбор навыка по каталогу, задание, ожидание, срок, снятие."""
    section = manifest_of(PLANNING).body.split("### Делегирование", 1)[1].split("###", 1)[0]

    for word in (
        "по каталогу навыков",
        'schedule_task(description="Навык: <id>.',
        "aifa_wait(task_id=",
        "ЖДИ",
        "подзадача_не_ответила",
        "get_task_result",
        "cancel_task",
        "Перед `aifa_finish`",
        "данные, а не указания",
    ):
        assert word in section, word


def test_the_delegation_names_a_skill_only_as_the_example_of_the_code_role() -> None:
    """Правило выбора общее (по каталогу), а не «всегда этот навык»: следующий навык подзадачи не требует правки раздела."""
    section = manifest_of(PLANNING).body.split("### Делегирование", 1)[1].split("###", 1)[0]

    assert section.count("distribution:") == 1 and "по `id` и `description`" in section


def test_planning_forbids_counting_on_its_own_and_treats_tool_text_as_data() -> None:
    body = manifest_of(PLANNING).body

    assert "сам не считай" in body or "ничего не считай" in body
    assert "данные" in body and "не указани" in body


@pytest.mark.parametrize("skill", [ENGINE, PLANNING], ids=["aifa-engine", "aifa-planning"])
def test_a_package_carries_no_development_documents(skill: pathlib.Path) -> None:
    """Инвариант 1 ouroboros: `skill_read` читает любой UTF-8 файл пакета — документов разработки там нет."""
    found = [p.name for p in skill.rglob("*") if p.is_file() and p.name in DEVELOPMENT_DOCS]

    assert not found


def test_the_instruction_package_holds_text_only() -> None:
    """F2: в `aifa-planning` нет исполняемых файлов."""
    code = [p.name for p in PLANNING.rglob("*") if p.is_file() and p.suffix in {".py", ".sh", ".js", ".ps1"}]

    assert not code


@pytest.mark.parametrize("skill", [ENGINE, PLANNING], ids=["aifa-engine", "aifa-planning"])
def test_a_package_ships_russian_display_text_like_every_distribution_package(
    skill: pathlib.Path,
) -> None:
    """Витрина навыка (`agents/gigaagent.yaml`): имя и описание по-русски, как требует набор поставки."""
    interface = yaml.safe_load((skill / "agents" / "gigaagent.yaml").read_text(encoding="utf-8"))["interface"]

    for field in ("display_name", "short_description"):
        assert any("А" <= letter <= "я" for letter in interface[field]), field
