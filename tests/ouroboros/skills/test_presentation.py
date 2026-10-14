"""Optional display metadata is independent of runtime validation and safe to serve."""

from new_core.skills.providers.distribution import DistributionSkillProvider
from ouroboros.skills.loader import DistributionSkillLoader
from ouroboros.skills.presentation import read_skill_presentation
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.parametrize import parametrize_from_dict


def _provider(tmp_path):
    package = tmp_path / "skills" / "example"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: example\ndescription: Original description\ntype: instruction\n---\nOriginal instructions\n"
    )
    provider = DistributionSkillProvider(
        distribution_root=package.parent,
        loader=DistributionSkillLoader(AppSettings()),
    )
    provider.refresh()
    return package, provider


@parametrize_from_dict(
    {
        "valid": {
            "metadata": "interface:\n  display_name: Пример\n  short_description: Описание\npermissions: [net]\ntype: tool\n"
        },
        "malformed": {"metadata": "interface: [not valid YAML"},
        "oversized": {"metadata": "interface: " + "x" * (256 * 1024)},
    }
)
def test_display_metadata_never_changes_the_runtime_package_snapshot(tmp_path, metadata):
    package, provider = _provider(tmp_path)
    (package / "metadata-alias.yaml").symlink_to("agents/gigaagent.yaml")
    (package / "uppercase-alias.yaml").symlink_to("AGENTS/GIGAAGENT.YAML")
    baseline = provider.skills()
    (package / "agents").mkdir()
    (package / "agents" / "gigaagent.yaml").write_text(metadata)
    provider.refresh()
    assert provider.skills() == baseline


def test_presentation_reads_text_and_falls_back_on_missing_or_invalid_metadata(tmp_path):
    package, provider = _provider(tmp_path)
    skill = provider.skills()[0]
    absent = read_skill_presentation(skill)
    assert (absent.display_name, absent.short_description) == ("example", "Original description")
    (package / "agents").mkdir()
    (package / "agents" / "gigaagent.yaml").write_text(
        "interface:\n  display_name: Пример\n  short_description: Краткое описание\n"
    )
    shown = read_skill_presentation(skill)
    assert (shown.display_name, shown.short_description) == ("Пример", "Краткое описание")
    (package / "agents" / "gigaagent.yaml").write_text("interface: [bad")
    assert read_skill_presentation(skill) == absent


@parametrize_from_dict(
    {
        "invalid display timestamp": {"metadata": "interface:\n  display_name: 2026-99-99\n"},
        "invalid ignored timestamp": {"metadata": "ignored: 2026-99-99\n"},
        "non-string fields": {"metadata": "interface:\n  display_name: [one]\n  short_description: 42\n"},
    }
)
def test_invalid_optional_yaml_values_fall_back(tmp_path, metadata):
    package, provider = _provider(tmp_path)
    skill = provider.skills()[0]
    fallback = read_skill_presentation(skill)
    (package / "agents").mkdir()
    (package / "agents" / "gigaagent.yaml").write_text(metadata)
    assert read_skill_presentation(skill) == fallback


def test_metadata_symlink_cannot_read_outside_the_package(tmp_path):
    package, provider = _provider(tmp_path)
    skill = provider.skills()[0]
    fallback = read_skill_presentation(skill)
    outside = package.parent / "outside.yaml"
    outside.write_text("interface:\n  display_name: Private\n")
    (package / "agents").mkdir()
    (package / "agents" / "gigaagent.yaml").symlink_to(outside)
    assert read_skill_presentation(skill) == fallback
