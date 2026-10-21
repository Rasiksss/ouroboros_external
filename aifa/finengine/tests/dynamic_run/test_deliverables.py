"""К16.4: файл, собранный в цикле, — результат ответа; `finalize` ссылается на него в `deliverables` (часть 16)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest

from finengine.config import settings
from finengine.contracts import FileArtifact, GoalIR, NodeKey
from finengine.skills.spec.dynamic_run import DynamicRun, parse_decision
from finengine.skills.spec.ephemeral import AtomRequest, EphemeralAtom
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import InputSpec, Target
from subtask_support import delegate
from test_dynamic_run import DynamicScenario, SequenceAuthor, _atom_doc
from test_ephemeral_in_graph import CASE, ENTITY, OWNER

BOOK_CODE = '''
def run(inputs):
    import os
    from openpyxl import Workbook
    os.makedirs("out", exist_ok=True)
    wb = Workbook()
    wb.active.title = "Данные"
    wb.active.append(["Показатель", "Значение"])
    wb.active.append(["Выручка", inputs["dyn:seed"][0]["payload"]["value"]])
    wb.active.append(["Себестоимость", 40])
    calc = wb.create_sheet("Расчёт")
    calc["A1"] = "Маржа"
    calc["B1"] = "=Данные!B2/Данные!B3"
    wb.save("out/итоги.xlsx")
    return {"filename": "итоги.xlsx"}
'''
REQUEST = AtomRequest(
    name="eph.build_book", purpose="книга Excel с формулами на двух листах", output_type="dyn:book",
    output_schema={"type": "object"}, requires=(InputSpec("dyn:seed"),),
)


@pytest.fixture(autouse=True)
def _local_blobs(tmp_path, monkeypatch, catalog):  # оригиналы файлов — в подставном каталоге ouroboros (шаг 11)
    monkeypatch.setattr(settings, "s3_endpoint", "")
    monkeypatch.setattr(settings, "data_dir", tmp_path)


@dataclass
class Planner:
    """Планировщик по ролям: ход 1 собирает книгу, затем действует по `finish(prompt)` на состоянии хода."""

    finish: object
    prompts: list[str] = field(default_factory=list)

    async def __call__(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return _move()
        return self.finish(prompt)


def _move() -> str:
    return json.dumps({
        "objective": "собрать Excel", "reason": "нужен файл", "action": "execute",
        "scenarios": ["Извлеки"], "new_atoms": [_atom_doc(REQUEST)], "steps": [{"atom": REQUEST.name}],
        "answer": "", "evidence_ids": [], "deliverables": [],
    }, ensure_ascii=False)


def _terminal(action: str = "finalize", *, evidence_ids=(), deliverables=(), answer: str = "Книга собрана.") -> str:
    return json.dumps({
        "objective": "ответить", "reason": "итог", "action": action, "scenarios": [], "new_atoms": [],
        "steps": [], "answer": answer, "evidence_ids": list(evidence_ids), "deliverables": list(deliverables),
    }, ensure_ascii=False)


def _file_id(prompt: str) -> str:
    """Как настоящий планировщик: id файла — первый столбец строки FileArtifact таблицы артефактов."""
    row = next(line for line in prompt.split("## Полученные артефакты")[1].splitlines() if "FileArtifact" in line)
    return row.split("|")[0].strip()


def _dynamic(base, planner, **kwargs) -> DynamicRun:
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="собери Excel", intent_kind="custom_analysis"),
        target=Target(output_type="NarrativeOutput", entity_ref=ENTITY),
        inputs=RunInputs(extra={"owner_id": OWNER, "project_id": CASE}),
        run_scenario=DynamicScenario(base), complete_fn=planner, **kwargs,
    )
    return delegate(run, SequenceAuthor([(EphemeralAtom(REQUEST, BOOK_CODE), None)]))


def _file_artifact() -> FileArtifact:
    return FileArtifact(
        run_id="run-1", file_kind="xlsx", mime_type="application/octet-stream", filename="итоги.xlsx",
        template_id="out.file", template_version="1",
        source_result={
            "result_type": "FormOutput", "result_id": "f-1", "schema_version": "1.0", "run_id": "run-1",
            "evidence_version": "1.0", "result_hash": "0" * 64,
        },
        content_hash="a" * 64, object_store_location="users/1/projects/p/sources/x.xlsx", size_bytes=10,
    )


def _events(dynamic: DynamicRun) -> list[dict]:
    events: list[dict] = []
    dynamic.event_sink = events.append
    return events


def test_a_planner_finalizes_with_the_file_it_built_by_its_id_from_the_state_table(base, local_room) -> None:
    planner = Planner(lambda prompt: _terminal(deliverables=[_file_id(prompt)]))
    dynamic = _dynamic(base, planner)

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "finalized" and result.evidence == ()
    assert [item.filename for item in result.deliverables] == ["итоги.xlsx"]
    assert isinstance(result.deliverables[0], FileArtifact)
    delivered = result.manifest()["deliverables"]
    assert delivered[0]["filename"] == "итоги.xlsx" and delivered[0]["file_kind"] == "xlsx"


def test_the_state_table_lists_the_file_with_its_id_name_and_structure(base, local_room) -> None:
    planner = Planner(lambda prompt: _terminal(deliverables=[_file_id(prompt)]))
    _dynamic(base, planner).run(deterministic_error="нет пути")

    state = planner.prompts[1]
    row = next(line for line in state.split("## Полученные артефакты")[1].splitlines() if "FileArtifact" in line)

    assert "итоги.xlsx" in row and "листов 2" in row and "формул 1" in row
    assert re.fullmatch(r"[0-9a-f-]{36}", row.split("|")[0].strip())
    assert "файлы:" in state.split("## Итог прошлого хода")[1] and "итоги.xlsx" in state.split("## Итог прошлого хода")[1]


def test_a_file_artifact_counts_toward_the_progress_of_the_cycle(base) -> None:
    """Узел `out.file` даёт только `FileArtifact` (без `evidence_id`): раньше такой ход считался пустым."""
    dynamic = _dynamic(base, Planner(lambda prompt: _terminal()))
    before = dynamic._artifact_count()

    base.store.put_artifact(
        OWNER, CASE, node_key=NodeKey(atom_name="out.file", atom_version="1", args_fingerprint="f"),
        output_type="FileArtifact", payloads=(_file_artifact(),), concept=None, entity_ref=ENTITY,
        period_ref=None, basis=None, document_version="sha256:v1", policy_version=base.policy.policy_version,
    )

    assert dynamic._artifact_count() == before + 1


def test_the_file_id_in_evidence_ids_is_accepted_as_a_deliverable(base, local_room) -> None:
    """Приёмка 06.10.2026: планировщик сослался на id файла как на evidence и дважды получил «не существует»."""
    planner = Planner(lambda prompt: _terminal(evidence_ids=[_file_id(prompt)]))

    result = _dynamic(base, planner).run(deterministic_error="нет пути")

    assert result.status == "finalized" and len(result.deliverables) == 1


def test_an_unknown_file_id_is_rejected_with_a_reason_the_planner_can_act_on(base, local_room) -> None:
    replies = iter([_terminal(deliverables=["нет-такого-файла"]), None])

    @dataclass
    class Script2:
        prompts: list = field(default_factory=list)

        async def __call__(self, system, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return _move()
            return next(replies) or _terminal("give_up", answer="Не вышло.")

    planner = Script2()
    dynamic = _dynamic(base, planner)
    events = _events(dynamic)

    result = dynamic.run(deterministic_error="нет пути")

    rejections = [event["details"] for event in events if event.get("status") == "rejected"]
    assert any("несуществующие файлы" in text and "нет-такого-файла" in text for text in rejections)
    assert result.status == "gave_up"


def test_a_file_built_in_this_run_is_not_thrown_away_when_the_planner_gives_up(base, local_room) -> None:
    planner = Planner(lambda prompt: _terminal("give_up", answer="Формулы не сошлись, вот что есть."))

    result = _dynamic(base, planner).run(deterministic_error="нет пути")

    assert result.status == "gave_up" and [item.filename for item in result.deliverables] == ["итоги.xlsx"]


def test_the_decision_schema_and_parser_know_deliverables() -> None:
    raw = _terminal(deliverables=["f-1", "f-2"])

    decision = parse_decision(raw, scenarios_enabled=True)

    assert decision.deliverables == ("f-1", "f-2")
    with pytest.raises(ValueError, match="deliverables"):
        parse_decision(_terminal().replace('"deliverables": []', '"deliverables": "f-1"'), scenarios_enabled=True)


def test_the_planner_prompt_and_the_response_schema_agree_on_deliverables() -> None:
    """К16.6: поле решения, пример ответа и правило промпта не расходятся (формулировки не сверяем)."""
    from finengine.skills.spec import dynamic_run

    prompt = dynamic_run._SKILL.read_text(encoding="utf-8")
    schema = dynamic_run._PLANNER_RESPONSE_FORMAT["json_schema"]["schema"]
    example = json.loads(prompt.split("```json")[1].split("```")[0])

    assert "deliverables" in schema["required"] and "deliverables" in schema["properties"]
    assert example["deliverables"] == [] and set(example) == set(schema["required"])
    assert "deliverables" in prompt and "FileArtifact" in prompt and "out/" in prompt


def test_the_author_skill_promises_only_libraries_the_station_image_installs() -> None:
    """К16.7: окружение комнаты описано в `aifa/deploy/dev/workstation/Dockerfile`, навык автора не обещает лишнего."""
    from pathlib import Path

    skill = Path(__file__).parents[4] / "skills" / "aifa-atom-author" / "SKILL.md"
    prompt = skill.read_text(encoding="utf-8")
    image = (Path(__file__).parents[3] / "deploy" / "dev" / "workstation" / "Dockerfile").read_text(encoding="utf-8")
    promised = ("openpyxl", "pandas", "numpy", "python-pptx", "python-docx")

    for library in promised:
        assert library in prompt, f"{library}: автор не знает о библиотеке"
        assert library in image, f"{library}: библиотеки нет в образе станции"
    assert "out/" in prompt, "автор не знает, куда писать файлы"


# ───────────────────────── К16.5: доставка в чат ─────────────────────────

def _outcome_events(base, planner) -> tuple[dict, list[dict]]:
    from finengine.skills.production_runtime import _dynamic_outcome

    result = _dynamic(base, planner).run(deterministic_error="нет пути")
    events: list[dict] = []
    outcome = _dynamic_outcome(result, events.append, case_id=CASE, document_id="doc-1")
    return outcome.manifest, events


def test_the_outcome_publishes_the_file_as_a_typed_result_and_a_download_event(base, local_room) -> None:
    import io
    import zipfile

    from finengine.skills import sources

    manifest, events = _outcome_events(base, Planner(lambda prompt: _terminal(deliverables=[_file_id(prompt)])))

    typed = [event for event in events if event["type"] == "typed_result"]
    downloads = [event for event in events if event["type"] == "file"]
    assert [event["result"]["output_type"] for event in typed] == ["FileArtifact"]
    assert len(downloads) == 1 and downloads[0]["filename"] == "итоги.xlsx" and downloads[0]["kind"] == "xlsx"
    content, meta = sources.read_source_bytes(OWNER, CASE, downloads[0]["file_id"])
    assert meta.filename == "итоги.xlsx" and zipfile.is_zipfile(io.BytesIO(content))
    assert manifest["dynamic_run"]["deliverables"][0]["filename"] == "итоги.xlsx"


def test_a_cycle_without_files_publishes_no_download_event(base, local_room) -> None:
    class Plain:
        def __init__(self):
            self.prompts = []

        async def __call__(self, system, prompt):
            self.prompts.append(prompt)
            return _terminal("give_up", answer="Данных нет.")

    from finengine.skills.production_runtime import _dynamic_outcome

    result = _dynamic(base, Plain()).run(deterministic_error="нет пути")
    events: list[dict] = []
    _dynamic_outcome(result, events.append, case_id=CASE, document_id="doc-1")

    assert [event for event in events if event["type"] == "file"] == []


def test_graph_runs_get_the_real_owner_and_the_project_of_the_chat_for_their_files() -> None:
    from finengine.skills.production_runtime import ProductionContext, _run_inputs

    context = ProductionContext(
        runtime=None, passport=None, owner_id=7, case_id="chat-1", document_id="doc-1", document_version="v1",
    )

    inputs = _run_inputs(context, document_text="текст", role="other", extra={"requested_concepts": "EBITDA"})

    assert inputs.extra["owner_id"] == "7" and inputs.extra["project_id"] == "chat-1"
    assert inputs.extra["requested_concepts"] == "EBITDA" and inputs.document_text == "текст" and inputs.role == "other"
