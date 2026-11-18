"""Справочники навыка `aifa-planning`, которые генерирует служба (шаг 14, PRINCIPLES.md §Б.3).

Каталог атомов и общий реестр показателей лежат в `skills/aifa-planning/references/` и пишутся только
отсюда: руками их не правят, а тест `tests/skill_references` краснеет, пока файлы не совпадают с выводом.
Запуск: `python -m finengine.assistant.skill_references [каталог] [--check]`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile
from finengine.skills.spec.atom_catalog import catalog_text

STATE_SECTION = "Реестр показателей"


def skill_references_dir() -> Path:
    """Каталог ссылок навыка в исходниках: корень репозитория — на пять уровней выше этого файла (в образе службы его нет)."""
    return Path(__file__).resolve().parents[5] / "skills" / "aifa-planning" / "references"


def atoms_reference() -> str:
    """Каталог атомов не зависит от профиля отчёта: тот же текст видит модель службы в сообщении хода."""
    return f"# Каталог атомов\n\n{catalog_text(build_registry())}\n"


def metrics_reference() -> str:
    """Показатели, которые есть в каждом профиле отчёта; профильный список остаётся в `aifa_state`."""
    registries = {profile: registry_for_profile(profile) for profile in PROFILE_IDS}
    extractable = _common_names(registries, "extractable")
    calculated = _common_calculated(registries)
    lines = [
        "# Показатели реестра: общее для всех профилей отчёта",
        "",
        "Реестр зависит от профиля отчёта (стандарт, тип компании, язык). Здесь — только показатели, "
        f"которые есть в каждом профиле. Полный список для отчёта этого хода — раздел «{STATE_SECTION}» "
        "в ответе `aifa_state`: верь ему, а не этому файлу.",
        "",
        "## Профили",
    ]
    lines += [
        f"- {profile} — тип компании {registry['company_type']}; извлекаемых {len(registry['extractable'])}, "
        f"рассчитываемых {len(registry['calculated'])}"
        for profile, registry in registries.items()
    ]
    lines += ["", "## Извлекаются во всех профилях (читаются из отчёта, если строка в нём есть)"]
    lines += [f"- {enum} — {name}" for enum, name in extractable]
    lines += ["", "## Рассчитываются во всех профилях (формула реестра над извлекаемыми)"]
    lines += [f"- {enum} — {name} = {formula}" for enum, name, formula in calculated]
    return "\n".join(lines) + "\n"


def _common_names(registries: Mapping[str, dict], kind: str) -> list[tuple[str, str]]:
    """Перечисления, которые есть во всех профилях, в порядке первого профиля."""
    first = next(iter(registries.values()))
    common = set.intersection(*(_enums(registry, kind) for registry in registries.values()))
    return [(metric["enum"], metric["name_ru"]) for metric in first[kind] if metric["enum"] in common]


def _common_calculated(registries: Mapping[str, dict]) -> list[tuple[str, str, str]]:
    """Рассчитываемые общие для всех профилей; формула, различная по профилям, не называется."""
    first, *rest = registries.values()
    common = set.intersection(*(_enums(registry, "calculated") for registry in registries.values()))
    rows = []
    for enum, name, formula, _ in first["calculated"]:
        if enum not in common:
            continue
        same = all(next(row for row in registry["calculated"] if row[0] == enum)[2] == formula for registry in rest)
        rows.append((enum, name, formula if same else "формула зависит от профиля — см. aifa_state"))
    return rows


def _enums(registry: dict, kind: str) -> set[str]:
    return {item["enum"] if isinstance(item, dict) else item[0] for item in registry[kind]}


REFERENCES: Mapping[str, Callable[[], str]] = {
    "atoms.md": atoms_reference,
    "metrics.md": metrics_reference,
}


def write_all(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name, generate in REFERENCES.items():
        (target / name).write_text(generate(), encoding="utf-8", newline="\n")


def stale_files(target: Path) -> list[str]:
    """Имена файлов, которых нет или которые расходятся с выводом генератора."""
    return [
        name
        for name, generate in REFERENCES.items()
        if not (target / name).is_file() or (target / name).read_text(encoding="utf-8") != generate()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Справочники навыка aifa-planning из реестров службы")
    parser.add_argument("target", nargs="?", type=Path, default=skill_references_dir())
    parser.add_argument("--check", action="store_true", help="не писать, а сообщить об устаревших файлах")
    args = parser.parse_args(argv)
    if not args.check:
        write_all(args.target)
        return 0
    stale = stale_files(args.target)
    for name in stale:
        print(f"устарело: {name}", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
