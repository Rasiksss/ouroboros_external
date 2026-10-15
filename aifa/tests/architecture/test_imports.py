"""F2 — границы импортов (ADR-0003, ADR-0012, ADR-0019, ADR-0020, ADR-0022).

Разбор `ast` всех `.py`; служба и ouroboros связываются контрактом, а не импортом. Правила:
1. `aifa/finengine/**` не импортирует код ouroboros;
2. код ouroboros (всё вне `aifa/` и `skills/aifa-*`) не импортирует `finengine`;
3. `skills/aifa-engine/` (без `tests/`) — только стандартная библиотека, `httpx` и
   `ouroboros.observability.aef_tracing`;
4. `skills/aifa-planning/`, `skills/aifa-atom-author/` — навыки-инструкции, без кода;
5. `boto3`/`botocore` в службе — только в `store/blobs.py` (её собственное хранилище блобов);
   каталог файлов ouroboros — через HTTP (ADR-0012).

Динамические импорты (`importlib`, строки) этот разбор не видит. Правила для `skills/aifa-*`
действуют, когда каталоги появятся: пустое множество файлов — зелёная проверка.
"""

from __future__ import annotations

import sys
from pathlib import Path

from arch_support import REPO_ROOT, absolute_imports, python_files, top_level

SERVICE = "aifa/finengine"
# ADR-0032: вендорная копия пакета `finengine` в benchmarks — не код службы.
VENDORED = ("aifa/finengine/benchmarks/vendor/src/finengine",)
CORE_PACKAGES = frozenset(
    {
        "ouroboros",
        "new_core",
        "transport",
        "supervisor",
        "config_registry",
        "infra",
        "multiuser_config",
        "background_memory",
    }
)
ENGINE_SKILL = "skills/aifa-engine"
ENGINE_SKILL_EXTRA = ("httpx",)
ENGINE_SKILL_TRACING = "ouroboros.observability.aef_tracing"
INSTRUCTION_SKILLS = ("skills/aifa-planning", "skills/aifa-atom-author")
CODE_SUFFIXES = frozenset({".py", ".sh", ".bash", ".js", ".mjs", ".ts", ".ps1", ".bat"})
S3_CLIENT_PACKAGES = frozenset({"boto3", "botocore", "aioboto3", "s3transfer"})
S3_ALLOWED_FILES = frozenset({"aifa/finengine/src/finengine/store/blobs.py"})


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def service_imports_core(root: Path) -> list[str]:
    problems = []
    for path in python_files(root, SERVICE, skip=VENDORED):
        for name, line in absolute_imports(path):
            if top_level(name) in CORE_PACKAGES:
                problems.append(
                    f"F2 (ADR-0003, ADR-0019): служба импортирует код ouroboros — "
                    f"{_rel(root, path)}:{line} `{name}`. Служба говорит с ouroboros по HTTP/WS "
                    "через контракт, не импортом."
                )
    return problems


def core_imports_service(root: Path) -> list[str]:
    # Всё вне `aifa/`: код ouroboros, его тесты и дистрибутивные навыки; `skills/aifa-*` — наши.
    skills = (root / "skills").glob("aifa-*")
    ours = ("aifa", "aifa-symbiosis", *(_rel(root, d) for d in skills if d.is_dir()))
    problems = []
    for path in python_files(root, skip=ours):
        for name, line in absolute_imports(path):
            if top_level(name) == "finengine":
                problems.append(
                    f"F2 (ADR-0003, ADR-0019): код ouroboros импортирует службу — "
                    f"{_rel(root, path)}:{line} `{name}`. Ouroboros зовёт службу только "
                    "навыком `aifa-engine` по HTTP."
                )
    return problems


def _engine_import_allowed(dotted: str) -> bool:
    top = top_level(dotted)
    if top in sys.stdlib_module_names or top in ENGINE_SKILL_EXTRA:
        return True
    return dotted == ENGINE_SKILL_TRACING or dotted.startswith(ENGINE_SKILL_TRACING + ".")


def engine_skill_is_thin(root: Path) -> list[str]:
    problems = []
    for path in python_files(root, ENGINE_SKILL, skip=(f"{ENGINE_SKILL}/tests",)):
        for name, line in absolute_imports(path):
            if not _engine_import_allowed(name):
                problems.append(
                    f"F2 (ADR-0020, ADR-0022): навык aifa-engine импортирует `{name}` — "
                    f"{_rel(root, path)}:{line}. Разрешены стандартная библиотека, httpx и "
                    f"{ENGINE_SKILL_TRACING}: навык — тонкий клиент без логики и зависимостей."
                )
    return problems


def instruction_skills_have_no_code(root: Path) -> list[str]:
    problems = []
    for skill in INSTRUCTION_SKILLS:
        base = root / skill
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in CODE_SUFFIXES:
                problems.append(
                    f"F2 (ADR-0019, ADR-0022): в навыке-инструкции {skill} лежит код — "
                    f"{_rel(root, path)}. Логика — в службе, навык только описывает порядок."
                )
    return problems


def s3_client_only_in_blob_store(root: Path) -> list[str]:
    problems = []
    for path in python_files(root, SERVICE, skip=VENDORED):
        rel = _rel(root, path)
        if rel in S3_ALLOWED_FILES:
            continue
        for name, line in absolute_imports(path):
            if top_level(name) in S3_CLIENT_PACKAGES:
                problems.append(
                    f"F2 (ADR-0012): служба импортирует S3-клиент `{name}` — {rel}:{line}. "
                    "Файлы ouroboros — только через HTTP каталога (`/api/files`); собственное "
                    f"хранилище службы — в {', '.join(sorted(S3_ALLOWED_FILES))}."
                )
    return problems


def _write(root: Path, rel: str, text: str = "x = 1\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_service_does_not_import_ouroboros() -> None:
    """F2: служба не импортирует код ouroboros."""
    problems = service_imports_core(REPO_ROOT)
    assert not problems, "\n".join(problems)


def test_ouroboros_does_not_import_the_service() -> None:
    """F2: код ouroboros не импортирует `finengine`."""
    problems = core_imports_service(REPO_ROOT)
    assert not problems, "\n".join(problems)


def test_engine_skill_imports_only_the_allowed_modules() -> None:
    """F2: навык aifa-engine — только стандартная библиотека, httpx и aef_tracing."""
    problems = engine_skill_is_thin(REPO_ROOT)
    assert not problems, "\n".join(problems)


def test_instruction_skills_carry_no_code() -> None:
    """F2: aifa-planning и aifa-atom-author — без кода."""
    problems = instruction_skills_have_no_code(REPO_ROOT)
    assert not problems, "\n".join(problems)


def test_s3_client_is_imported_only_by_the_blob_store() -> None:
    """F2: S3-клиент в службе — только в её хранилище блобов."""
    problems = s3_client_only_in_blob_store(REPO_ROOT)
    assert not problems, "\n".join(problems)


def test_f2_catches_the_service_importing_ouroboros(tmp_path: Path) -> None:
    """F2: нарушитель — `import`, `from … import` и вложенный импорт кода ouroboros в службе."""
    _write(tmp_path, "aifa/finengine/src/finengine/a.py", "import ouroboros.agent\n")
    _write(tmp_path, "aifa/finengine/src/finengine/b.py", "from supervisor.queue import Q\n")
    _write(tmp_path, "aifa/finengine/tests/c.py", "def f():\n    import new_core\n")
    problems = service_imports_core(tmp_path)
    assert len(problems) == 3
    assert all("ADR-0003" in p and "ADR-0019" in p for p in problems)
    assert "a.py:1" in problems[0]


def test_f2_does_not_flag_look_alike_names_or_the_vendored_copy(tmp_path: Path) -> None:
    """F2: `finengine.transport` и вендорная копия пакета — не нарушение."""
    _write(tmp_path, "aifa/finengine/src/finengine/ok.py", "from finengine.transport import x\n")
    _write(tmp_path, "aifa/finengine/src/finengine/rel.py", "from . import infra\n")
    vendored = "aifa/finengine/benchmarks/vendor/src/finengine/v.py"
    _write(tmp_path, vendored, "import ouroboros\n")
    assert service_imports_core(tmp_path) == []


def test_f2_catches_ouroboros_importing_the_service(tmp_path: Path) -> None:
    """F2: нарушитель — код ouroboros и его навык импортируют `finengine`."""
    _write(tmp_path, "ouroboros/tools/x.py", "from finengine.llm import tools\n")
    _write(tmp_path, "skills/other/plugin.py", "import finengine\n")
    problems = core_imports_service(tmp_path)
    assert len(problems) == 2
    assert all("ADR-0019" in p for p in problems)


def test_f2_lets_our_own_paths_import_the_service(tmp_path: Path) -> None:
    """F2: `aifa/` и `skills/aifa-*` под это правило не подпадают."""
    _write(tmp_path, "aifa/tests/t.py", "import finengine\n")
    _write(tmp_path, "skills/aifa-engine/tests/t.py", "import finengine\n")
    assert core_imports_service(tmp_path) == []


def test_f2_catches_a_foreign_import_in_the_engine_skill(tmp_path: Path) -> None:
    """F2: нарушитель — навык aifa-engine тянет `pydantic`, `requests` и чужую часть ouroboros."""
    _write(tmp_path, "skills/aifa-engine/plugin.py", "import pydantic\nimport requests\n")
    _write(tmp_path, "skills/aifa-engine/more.py", "from ouroboros.agent import loop\n")
    problems = engine_skill_is_thin(tmp_path)
    assert len(problems) == 3
    assert all("ADR-0020" in p and "ADR-0022" in p for p in problems)


def test_f2_accepts_the_allowed_engine_skill_imports(tmp_path: Path) -> None:
    """F2: stdlib, httpx, aef_tracing (любая форма импорта), относительные и тесты — зелёные."""
    source = (
        "import json\n"
        "from pathlib import Path\n"
        "import httpx\n"
        "from . import helpers\n"
        "from ouroboros.observability import aef_tracing\n"
        "from ouroboros.observability.aef_tracing import span\n"
        "import ouroboros.observability.aef_tracing\n"
    )
    _write(tmp_path, "skills/aifa-engine/plugin.py", source)
    _write(tmp_path, "skills/aifa-engine/tests/test_x.py", "import pytest\n")
    assert engine_skill_is_thin(tmp_path) == []


def test_f2_is_green_when_the_aifa_skill_directories_do_not_exist_yet(tmp_path: Path) -> None:
    """F2: пустое множество файлов навыков — зелёная проверка, а не ошибка."""
    for rule in (engine_skill_is_thin, instruction_skills_have_no_code, core_imports_service):
        assert rule(tmp_path) == []


def test_f2_catches_code_in_an_instruction_skill(tmp_path: Path) -> None:
    """F2: нарушитель — скрипт в навыке-инструкции; документы рядом — не нарушение."""
    _write(tmp_path, "skills/aifa-planning/SKILL.md", "# навык\n")
    _write(tmp_path, "skills/aifa-planning/references/plan.md", "# план\n")
    _write(tmp_path, "skills/aifa-atom-author/scripts/run.py", "print(1)\n")
    problems = instruction_skills_have_no_code(tmp_path)
    assert len(problems) == 1
    assert "aifa-atom-author/scripts/run.py" in problems[0]
    assert "ADR-0022" in problems[0]


def test_f2_catches_an_s3_client_outside_the_blob_store(tmp_path: Path) -> None:
    """F2: нарушитель — boto3 в модуле, который ходит в каталог файлов; blobs.py разрешён."""
    _write(tmp_path, "aifa/finengine/src/finengine/store/blobs.py", "import boto3\n")
    _write(tmp_path, "aifa/finengine/src/finengine/ouro_client.py", "import boto3\n")
    problems = s3_client_only_in_blob_store(tmp_path)
    assert len(problems) == 1
    assert "ouro_client.py:1" in problems[0]
    assert "ADR-0012" in problems[0]
