"""F8 — каждое правило цикла держит тест (ADR-0023, ADR-0040, FITNESS.md).

`aifa/contracts/loop_rules.yaml` — карта «правило → тест». Проверка сверяет её с источниками правил: строки таблицы
раздела 8 плана 34 и фрагменты столбца «Правила» §3.1 `CONTRACTS.md` для инструментов цикла. Новое правило в
источнике без строки в карте — красный; строка карты без живого теста — красный.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
FINENGINE = ROOT / "aifa" / "finengine"
MAP = yaml.safe_load((ROOT / "aifa" / "contracts" / "loop_rules.yaml").read_text(encoding="utf-8"))
CONTRACTS = (ROOT / "aifa-symbiosis" / "architecture" / "CONTRACTS.md").read_text(encoding="utf-8")
LOOP_TOOLS = ("aifa_state", "aifa_doc", "aifa_move", "aifa_finish")
RULES = MAP["rules"]
DEFERRED = MAP["deferred"]
DEFERRED_STEPS = {"13a", "16", "17"}


def _plan34_rows() -> list[str]:
    text = (FINENGINE / "docs" / "build-plan" / "34-gibridnyy-planirovshchik-dynamic-run.md").read_text(encoding="utf-8")
    section = text.split("## 8. Правила, которые держит код, а не промпт", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    return [row.strip("|").split(" | ", 1)[0].strip() for row in rows[2:]]


def _split_top(cell: str) -> list[str]:
    """Фрагменты ячейки между `;` верхнего уровня: `;` внутри скобок и кавычек ячейку не делит."""
    parts, depth, current = [], 0, ""
    for char in cell:
        depth += char in "([{«"
        depth -= char in ")]}»"
        if char == ";" and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return [part for part in parts if part]


def _rules_cells() -> list[tuple[str, str]]:
    cells = []
    for line in CONTRACTS.splitlines():
        found = re.match(r"^\| `(aifa_[a-z_]+)` \|", line)
        if found and found.group(1) in LOOP_TOOLS:
            columns = [column.strip() for column in line.strip().strip("|").split(" | ")]
            cells += [(found.group(1), part) for part in _split_top(columns[-1])]
    return cells


def _test_function(spec: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    path, _, name = spec.partition("::")
    file = FINENGINE / path
    if not file.is_file():
        return None
    tree = ast.parse(file.read_text(encoding="utf-8"))
    return next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name),
        None,
    )


def test_every_row_of_the_plan_34_rules_table_is_mapped() -> None:
    """F8: строка раздела 8 плана 34 без правила в карте — правило держится только словами."""
    mapped = {rule["plan34"] for rule in RULES if "plan34" in rule}

    missing = [row for row in _plan34_rows() if row not in mapped]

    assert missing == [], f"F8 (ADR-0023): в `loop_rules.yaml` нет правил для строк плана 34: {missing}"


def test_every_fragment_of_the_rules_column_is_mapped_or_deferred_to_a_later_step() -> None:
    """F8: фрагмент столбца «Правила» инструмента цикла — либо правило с тестом, либо правило шага 13a/16/17."""
    known = {rule["cell"] for rule in RULES if "cell" in rule} | {item["cell"] for item in DEFERRED}

    missing = [f"{tool}: {cell}" for tool, cell in _rules_cells() if cell not in known]

    assert missing == [], f"F8 (ADR-0023): в карте нет фрагментов столбца «Правила»: {missing}"


def test_every_mapped_fragment_still_exists_in_the_contract() -> None:
    """F8: фрагмент карты, которого нет в контракте, — устаревшая строка: контракт сменился без пересмотра карты."""
    in_contract = {cell for _, cell in _rules_cells()}

    stale = [item["cell"] for item in [*RULES, *DEFERRED] if "cell" in item and item["cell"] not in in_contract]
    stale += [rule["contract"] for rule in RULES if "contract" in rule and rule["contract"] not in CONTRACTS]

    assert stale == [], f"F8: фрагменты карты, которых нет в `CONTRACTS.md`: {stale}"


def test_a_fragment_is_not_both_a_rule_and_a_deferred_one() -> None:
    mapped = {rule["cell"] for rule in RULES if "cell" in rule}

    assert [item["cell"] for item in DEFERRED if item["cell"] in mapped] == []
    assert {item["step"] for item in DEFERRED} <= DEFERRED_STEPS


def test_rule_ids_are_unique_and_every_rule_names_its_source_and_test() -> None:
    ids = [rule["id"] for rule in RULES]

    assert len(ids) == len(set(ids))
    assert all(({"plan34", "cell", "contract"} & rule.keys()) and rule.get("test") for rule in RULES)


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule["id"])
def test_the_test_of_every_rule_exists_and_pytest_collects_it(rule: dict) -> None:
    """F8: тест существует в названном файле, его имя собирает pytest и он не снят безусловно."""
    function = _test_function(rule["test"])

    assert function is not None, f"F8: нет теста {rule['test']} (правило {rule['id']})"
    assert function.name.startswith("test_")
    skipped = [
        decorator for decorator in function.decorator_list
        if "skip" in ast.unparse(decorator) and "skipif" not in ast.unparse(decorator)
    ]
    assert skipped == [], f"F8: тест правила {rule['id']} снят безусловно"


@pytest.mark.parametrize("rule", [rule for rule in RULES if "tests/loop_tools/" in rule["test"]], ids=lambda r: r["id"])
def test_a_test_through_the_tools_names_the_rule_it_closes(rule: dict) -> None:
    """F8: тест через API инструментов называет идентификатор правила в строке документации — по ней ревьюер находит покрытие."""
    function = _test_function(rule["test"])

    assert function is not None and rule["id"] in (ast.get_docstring(function) or ""), (
        f"F8: в докстринге {rule['test']} нет {rule['id']}"
    )
