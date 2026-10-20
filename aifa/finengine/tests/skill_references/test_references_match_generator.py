"""Ссылки навыка `aifa-planning` не расходятся с реестрами службы (шаг 14, PRINCIPLES.md §Б.3).

Каталог атомов и общий реестр показателей в `skills/aifa-planning/references/` генерирует служба:
руками их не пишут. Тест сверяет файлы навыка с выводом генератора, поэтому правка реестра без
перегенерации ссылок краснит набор.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from finengine.assistant import skill_references
from finengine.skills.spec import atom_catalog

SKILL = Path(__file__).resolve().parents[4] / "skills" / "aifa-planning"
REGENERATE = "python -m finengine.assistant.skill_references"


@pytest.mark.parametrize("name", sorted(skill_references.REFERENCES))
def test_a_generated_reference_equals_the_generator_output(name: str) -> None:
    """Дрейф: файл навыка совпадает с тем, что пишет генератор сейчас."""
    on_disk = (SKILL / "references" / name).read_text(encoding="utf-8")

    assert on_disk == skill_references.REFERENCES[name](), f"устарело: {name}; обновить — {REGENERATE}"


def test_a_changed_atom_description_makes_the_reference_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Защита от дрейфа сама проверяется: правка реестра атомов меняет вывод генератора."""
    before = skill_references.atoms_reference()
    monkeypatch.setitem(atom_catalog.ATOM_DESCRIPTIONS, "read.value", "Другое описание атома.")

    assert skill_references.atoms_reference() != before


def test_the_atom_reference_lists_every_plannable_atom_and_none_of_the_gates() -> None:
    reference = skill_references.atoms_reference()

    assert "- read.value " in reference and "- web.search " in reference
    for gate in ("- verify.bounds ", "- norm.unit ", "- ingest.document "):
        assert gate not in reference


def test_the_metric_reference_sends_the_profile_part_to_the_state() -> None:
    """Реестр зависит от профиля отчёта: в навыке — общее, профильное остаётся в `aifa_state`."""
    reference = skill_references.metrics_reference()

    assert "aifa_state" in reference and "Реестр показателей" in reference
    assert "ifrs_nf_ru" in reference


def test_every_reference_the_skill_names_exists_and_every_generated_one_is_named() -> None:
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"references/([\w.-]+\.md)", body))

    assert named, "SKILL.md не называет ни одной ссылки"
    for name in named:
        assert (SKILL / "references" / name).is_file(), name
    assert set(skill_references.REFERENCES) <= named


def test_check_mode_reports_the_stale_file(tmp_path: Path) -> None:
    (tmp_path / "atoms.md").write_text("устарело", encoding="utf-8")

    stale = skill_references.stale_files(tmp_path)

    assert "atoms.md" in stale and "metrics.md" in stale, "нет файла — тоже устарело"


def test_writing_makes_the_check_pass(tmp_path: Path) -> None:
    skill_references.write_all(tmp_path)

    assert skill_references.stale_files(tmp_path) == []
