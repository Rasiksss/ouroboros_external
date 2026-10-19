"""План цикла, гейт файла и сжатая таблица состояния (часть 16а).

Прогон владельца 06.10.2026 (case ef849d1f…): цикл получил показатели и сразу пошёл в `finalize`, не собрав
Excel; сослался на `output_id` формы и трижды получил «артефакт не существует».
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from finengine.config import settings
from finengine.contracts import FormOutput, NodeKey, VerificationFinding
from finengine.skills.spec import dynamic_run
from finengine.skills.spec.atom_author import AtomAuthor, render_author_prompt
from finengine.skills.spec.dynamic_run import parse_decision
from finengine.skills.spec.ephemeral import EphemeralAtom
from finengine.skills.spec.room_files import FileSpec
from test_atom_author import Script
from test_deliverables import BOOK_CODE, REQUEST, _dynamic, _events
from test_dynamic_run import SequenceAuthor, _atom_doc
from test_ephemeral_atoms import _metric
from test_ephemeral_in_graph import CASE, ENTITY, OWNER

PLAN = ["получить показатели", "собрать Excel на двух листах с формулами"]
BOOK = {"kind": "xlsx", "parts": 2, "formulas": True}
# Тот же атом, но расчёт записан числом: листов 2, формул 0 — так автор ответил на dev 07.10.2026.
PLAIN_BOOK_CODE = BOOK_CODE.replace('"=Данные!B2/Данные!B3"', "2.5")


@pytest.fixture(autouse=True)
def _local_blobs(tmp_path, monkeypatch, catalog):  # оригиналы файлов — в подставном каталоге ouroboros (шаг 11)
    monkeypatch.setattr(settings, "s3_endpoint", "")
    monkeypatch.setattr(settings, "data_dir", tmp_path)


def _reply(action: str = "execute", *, plan=(), files=(), scenarios=(), build: bool = False, answer: str = "",
           evidence_ids=()) -> str:
    return json.dumps({
        "objective": "ход", "reason": "нужно для ответа", "action": action, "plan": list(plan),
        "files": list(files), "scenarios": list(scenarios),
        "new_atoms": [_atom_doc(REQUEST)] if build else [], "steps": [{"atom": REQUEST.name}] if build else [],
        "answer": answer, "evidence_ids": list(evidence_ids), "deliverables": [],
    }, ensure_ascii=False)


@dataclass
class Replies:
    """Планировщик по сценарию: ответы по порядку; после последнего — `give_up`."""

    replies: list
    prompts: list[str] = field(default_factory=list)

    async def __call__(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) > len(self.replies):
            return _reply("give_up", answer="Не вышло.")
        reply = self.replies[len(self.prompts) - 1]
        return reply(prompt) if callable(reply) else reply


def _answer_by_seed(answer: str):
    """Ответ по результату сценария: его id — первый столбец строки `dyn:seed` таблицы артефактов."""
    def reply(prompt: str) -> str:
        row = next(line for line in _section(prompt, "Полученные артефакты").splitlines() if "dyn:seed" in line)
        return _reply("finalize", answer=answer, evidence_ids=[row.split("|")[0].strip()])
    return reply


def _rejections(events: list[dict]) -> list[str]:
    return [str(event["details"]) for event in events if event.get("status") == "rejected"]


def _section(prompt: str, title: str) -> str:
    return prompt.split(f"## {title}\n", 1)[1].split("\n\n## ", 1)[0]


def _put(base, output_type: str, payload, *, concept=None, atom: str = "seed") -> None:
    base.store.put_artifact(
        OWNER, CASE, node_key=NodeKey(atom_name=atom, atom_version="1", args_fingerprint=f"{output_type}:{concept}"),
        output_type=output_type, payloads=(payload,), concept=concept, entity_ref=ENTITY, period_ref=None,
        basis=None, document_version="sha256:v1", policy_version=base.policy.policy_version,
    )


# ───────────────────────── план ─────────────────────────

def test_the_decision_carries_a_plan_and_the_files_it_promises() -> None:
    decision = parse_decision(_reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]), scenarios_enabled=True)

    assert decision.plan == tuple(PLAN)
    assert decision.files == (FileSpec(kind="xlsx", parts=2, formulas=True),)
    with pytest.raises(ValueError, match="files"):
        parse_decision(_reply(files=[{"kind": "Excel-файл"}], scenarios=["Извлеки"]), scenarios_enabled=True)


def test_a_reply_recorded_before_the_plan_existed_still_parses() -> None:
    old = json.loads(_reply(scenarios=["Извлеки"]))
    del old["plan"], old["files"]

    decision = parse_decision(json.dumps(old, ensure_ascii=False), scenarios_enabled=True)

    assert decision.plan == () and decision.files == ()


def test_the_plan_of_the_first_move_is_shown_on_every_later_move(base, local_room) -> None:
    planner = Replies([_reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]), _reply(build=True)])

    _dynamic(base, planner).run(deterministic_error="нет пути")

    assert "плана нет" in _section(planner.prompts[0], "План")
    for prompt in planner.prompts[1:3]:
        plan = _section(prompt, "План")
        assert "1. получить показатели" in plan and "2. собрать Excel на двух листах с формулами" in plan
    assert "xlsx" in _section(planner.prompts[1], "План") and "не собран" in _section(planner.prompts[1], "План")
    assert "собран: итоги.xlsx" in _section(planner.prompts[2], "План")


def test_the_planner_prompt_and_the_response_schema_agree_on_the_plan() -> None:
    prompt = dynamic_run._SKILL.read_text(encoding="utf-8")
    schema = dynamic_run._PLANNER_RESPONSE_FORMAT["json_schema"]["schema"]
    example = json.loads(prompt.split("```json")[1].split("```")[0])

    assert {"plan", "files"} <= set(schema["required"]) and set(example) == set(schema["required"])
    assert set(schema["properties"]["files"]["items"]["required"]) == {"kind", "parts", "formulas"}
    # пример ответа файла не обещает (иначе модель обещала бы его всегда); форма `files` показана в тексте
    assert example["files"] == [] and example["plan"]
    shown = json.loads(prompt.split("с формулами» —\n  `", 1)[1].split("`", 1)[0])
    assert parse_decision(_reply(files=[shown], scenarios=["Извлеки"]), scenarios_enabled=True).files == (
        FileSpec("xlsx", 2, True),
    )
    assert "`plan`" in prompt and "`files`" in prompt


# ───────────────────────── гейт файла ─────────────────────────

def test_finalize_is_refused_while_the_promised_file_is_not_built(base, local_room) -> None:
    planner = Replies([
        _reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]),
        _reply("finalize", answer="Показатели получены."),
        _reply(build=True),
        _reply("finalize", answer="Книга собрана."),
    ])
    dynamic = _dynamic(base, planner)
    events = _events(dynamic)

    result = dynamic.run(deterministic_error="нет пути")

    refusal = _rejections(events)[0]
    assert "xlsx" in refusal and "new_atoms" in refusal and "out.file" in refusal
    assert result.status == "finalized"
    assert [item.filename for item in result.deliverables] == ["итоги.xlsx"], "файл по плану уходит в ответ сам"


def test_a_file_poorer_than_the_plan_does_not_pass_as_the_promised_one(base, local_room) -> None:
    planner = Replies([
        _reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]),
        _reply(build=True),
        _reply("finalize", answer="Книга собрана."),
    ])
    dynamic = _dynamic(base, planner)
    dynamic.author = SequenceAuthor([(EphemeralAtom(REQUEST, PLAIN_BOOK_CODE), None)])
    events = _events(dynamic)

    dynamic.run(deterministic_error="нет пути")

    assert any("итоги.xlsx" in text and "формул" in text for text in _rejections(events))


def test_after_two_failed_attempts_to_build_the_file_the_planner_may_answer_without_it(base, local_room) -> None:
    failed = (None, "ValueError: единицы несовместимы")
    planner = Replies([
        _reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]),
        _reply(build=True),
        _reply(build=True),
        _answer_by_seed("Показатели получены, файл собрать не удалось."),
    ])
    dynamic = _dynamic(base, planner)
    dynamic.author = SequenceAuthor([failed, failed])

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "finalized" and result.deliverables == ()
    assert result.answer == "Показатели получены, файл собрать не удалось."


def test_a_cycle_stopped_by_the_system_gets_its_last_word_without_the_file(base, local_room) -> None:
    planner = Replies([
        _reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]),
        "не JSON", "не JSON", "не JSON",
        _answer_by_seed("Показатели получены, файла нет."),
    ])

    result = _dynamic(base, planner).run(deterministic_error="нет пути")

    assert result.answer == "Показатели получены, файла нет."


# ───────────────────────── таблица состояния и отказы finalize ─────────────────────────

def _form() -> FormOutput:
    return FormOutput(run_id="run-form", role="other", rows=[])


def test_the_state_table_keeps_what_an_answer_can_cite_and_drops_the_plumbing(base, local_room) -> None:
    metric = _metric("EBITDA", 500.0)
    _put(base, "ExtractedMetric", metric, concept="EBITDA")
    passed = VerificationFinding(
        produced_by_atom="verify.bounds", produced_by_atom_version="1", source_ref=metric.scalar_fact.evidence_id,
        check_kind="bounds", status="pass", detail="в границах",
    )
    failed = passed.model_copy(update={"evidence_id": "finding-fail", "status": "fail", "detail": "вне границ"})
    _put(base, "VerificationFinding", passed, concept="pass")
    _put(base, "VerificationFinding", failed, concept="fail")
    _put(base, "CandidateLocator", metric, concept="locator")
    form = _form()
    _put(base, "FormOutput", form)
    planner = Replies([])

    _dynamic(base, planner).run(deterministic_error="нет пути")

    table = _section(planner.prompts[0], "Полученные артефакты")
    assert metric.scalar_fact.evidence_id in table and "EBITDA" in table
    assert "вне границ" in table and "в границах" not in table and "CandidateLocator" not in table
    form_row = next(line for line in table.splitlines() if "FormOutput" in line)
    assert form_row.startswith("— |") and form.output_id not in form_row and "run-form" not in form_row
    assert "не цитируется" in form_row


# ───────────────────────── автор ─────────────────────────

def test_the_author_sees_a_sample_of_every_input_type_not_the_head_of_the_first() -> None:
    inputs = {
        "ExtractedMetric": [{"concept": f"METRIC_{index}", "note": "x" * 400} for index in range(50)],
        "CalculatedMetric": [{"derived_fact": {"expression": "A / B", "inputs": ["id-1", "id-2"]}}],
    }

    prompt = render_author_prompt(REQUEST, "ВХОДЫ", inputs, None, None)

    assert "METRIC_0" in prompt and '"expression": "A / B"' in prompt
    assert "ExtractedMetric: 50" in prompt and "CalculatedMetric: 1" in prompt
    assert "METRIC_49" not in prompt


def test_the_author_is_told_what_the_file_must_contain_and_fixes_a_poorer_one(local_room) -> None:
    script = Script([json.dumps({"code": code, "example_inputs": {}}) for code in (PLAIN_BOOK_CODE, BOOK_CODE)])
    inputs = {"dyn:seed": [{"payload": {"value": 100}}]}

    atom, error = AtomAuthor(script).write(
        REQUEST, inputs_doc="ВХОДЫ", real_inputs=inputs, expected_files=(FileSpec("xlsx", 2, True),),
    )

    assert error is None and atom.code == BOOK_CODE
    assert "## Требования к файлу" in script.prompts[0] and "формул" in script.prompts[0]
    assert "итоги.xlsx" in script.prompts[1] and "формул" in script.prompts[1].split("## Ошибка")[1]


def test_a_calculation_atom_is_not_blamed_for_the_file_another_atom_will_build(local_room) -> None:
    code = "def run(inputs):\n    return {}\n"
    script = Script([json.dumps({"code": code, "example_inputs": {}})])

    atom, error = AtomAuthor(script).write(
        REQUEST, inputs_doc="ВХОДЫ", real_inputs={"dyn:seed": [{"payload": {"value": 1}}]},
        expected_files=(FileSpec("xlsx", 2, True),),
    )

    assert error is None and atom is not None


# ───────────────────────── вход «любой показатель» в графе ─────────────────────────

ALL_METRICS_CODE = """
def run(inputs):
    return {"concepts": sorted(item["concept"] for item in inputs["ExtractedMetric"])}
"""


def test_an_atom_that_takes_any_metric_gets_the_metrics_already_in_the_case(base, local_room) -> None:
    """dev 07.10.2026 (3a2a60c3): вход `ExtractedMetric:*` граф достраивал чтением документа без показателя,
    чтение падало, и готовый атом сборки Excel трижды не запустился."""
    from finengine.contracts import GoalIR
    from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
    from finengine.skills.spec.dynamic_run import DynamicRun
    from finengine.skills.spec.ephemeral import AtomRequest
    from finengine.skills.spec.graph_executor import RunInputs
    from finengine.skills.spec.target import InputSpec, Target
    from test_dynamic_run import ExtractScenario

    request = AtomRequest(
        name="eph.all_metrics", purpose="все показатели", output_type="dyn:all_metrics",
        output_schema={"type": "object"}, requires=(InputSpec("ExtractedMetric"),),
    )
    goal = GoalIR(raw_query="собери всё", intent_kind="custom_analysis")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell("ifrs_nf_ru"),
    )
    planner = Replies([
        _reply(scenarios=["Извлеки"]),
        json.dumps({
            "objective": "собрать", "reason": "нужно", "action": "execute", "plan": [], "files": [],
            "scenarios": [], "new_atoms": [_atom_doc(request)], "steps": [{"atom": request.name}],
            "answer": "", "evidence_ids": [], "deliverables": [],
        }, ensure_ascii=False),
    ])
    dynamic = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="dyn:all_metrics", entity_ref=ENTITY, period_ref="2024"),
        inputs=RunInputs(document_text="отчёт"), run_scenario=ExtractScenario(base, passport),
        complete_fn=planner, author=SequenceAuthor([(EphemeralAtom(request, ALL_METRICS_CODE), None)]),
        run_passport=passport,
    )

    dynamic.run(deterministic_error="нет пути")

    table = _section(planner.prompts[2], "Полученные артефакты")
    row = next(line for line in table.splitlines() if "dyn:all_metrics" in line)
    assert '"EBITDA"' in row and '"REVENUE"' in row, planner.prompts[2].split("## Итог прошлого хода")[1]


# ───────────────────────── формулы, которые Excel не вычислит ─────────────────────────

def _book(formula: str) -> bytes:
    import io

    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "Исходные данные"
    book.active["B2"] = 10
    book.active["B3"] = 4
    book.create_sheet("Расчёты")["B2"] = formula
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_a_formula_that_still_names_a_metric_instead_of_a_cell_is_the_authors_error() -> None:
    """dev 07.10.2026 (d320bfd2, 0de44660): `='Показатели'!C49 + LONG_TERM_LEASE_LIABILITIES` — Excel покажет #NAME?."""
    from finengine.skills.spec.room_files import files_problem
    from finengine.skills.spec.sealed_room import RoomFile

    broken = RoomFile("отчёт.xlsx", _book("='Исходные данные'!B2 + LONG_TERM_LEASE_LIABILITIES"))
    sound = RoomFile("отчёт.xlsx", _book("=SUM('Исходные данные'!B2:B3) / 'Исходные данные'!B3 * 100%"))

    problem = files_problem((broken,))

    assert problem and "LONG_TERM_LEASE_LIABILITIES" in problem and "#NAME?" in problem
    assert files_problem((sound,)) is None


# ───────────────────────── протокол ответа: чинить, а не отклонять ─────────────────────────
# dev 07.10.2026 (case 04be89e1): три отказа подряд по форме ответа — «output_schema невалидна», таймаут
# модели, «атом объявлен, но не вызван» — остановили цикл до сборки файла.

def _atom_move(*, steps, schema) -> str:
    atom = {**_atom_doc(REQUEST), "output_schema": schema}
    return json.dumps({
        "objective": "собрать", "reason": "нужен файл", "action": "execute", "plan": [], "files": [],
        "scenarios": [], "new_atoms": [atom], "steps": steps, "answer": "", "evidence_ids": [], "deliverables": [],
    }, ensure_ascii=False)


def test_a_declared_atom_without_a_step_is_scheduled_instead_of_refused() -> None:
    decision = parse_decision(_atom_move(steps=[], schema={"type": "object"}), scenarios_enabled=True)

    assert [step["atom"] for step in decision.raw_steps] == [REQUEST.name]


def test_a_broken_output_schema_of_an_atom_falls_back_to_any_object() -> None:
    broken = {"type": "object", "properties": {"filename": None}}

    decision = parse_decision(_atom_move(steps=[{"atom": REQUEST.name}], schema=broken), scenarios_enabled=True)
    missing = parse_decision(_atom_move(steps=[{"atom": REQUEST.name}], schema=None), scenarios_enabled=True)

    assert decision.new_atoms[0].output_schema == {"type": "object"} == missing.new_atoms[0].output_schema


def test_a_silent_model_does_not_count_as_a_refused_answer(base, local_room) -> None:
    class Flaky(Replies):
        async def __call__(self, system: str, prompt: str) -> str:
            reply = await super().__call__(system, prompt)
            if reply == "таймаут":
                raise TimeoutError
            return reply

    planner = Flaky([
        _reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]),
        "не JSON", "таймаут", "не JSON",
        _reply(build=True),
        _reply("finalize", answer="Книга собрана."),
    ])

    result = _dynamic(base, planner).run(deterministic_error="нет пути")

    assert result.status == "finalized" and [item.filename for item in result.deliverables] == ["итоги.xlsx"]


# ───────────────────────── файл собран: пора отвечать ─────────────────────────
# dev 07.10.2026 (c7452f91): файл собран на 5-й минуте, но планировщик пошёл считать EBITDA, трижды не уложился
# в таймаут, цикл остановлен по времени; в чат ушли оба файла (и ненужная выгрузка формы) с текстом «не удалось».

def _stray_file(base) -> None:
    from test_deliverables import _file_artifact

    _put(base, "FileArtifact", _file_artifact().model_copy(update={"summary": "xlsx, 10 байт; листов 1 (Форма); формул 0"}))


def test_once_the_promised_file_is_built_the_state_tells_the_planner_to_answer(base, local_room) -> None:
    planner = Replies([_reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]), _reply(build=True)])

    _dynamic(base, planner).run(deterministic_error="нет пути")

    assert "finalize" not in _section(planner.prompts[1], "План")
    assert "следующим ходом — finalize" in _section(planner.prompts[2], "План")


def test_a_stopped_cycle_hands_over_the_promised_file_and_says_so(base, local_room) -> None:
    class Stray(Replies):
        async def __call__(self, system: str, prompt: str) -> str:
            if len(self.prompts) == 2:
                _stray_file(base)           # лишний файл этого же прогона: выгрузка формы одним листом
            return await super().__call__(system, prompt)

    planner = Stray([
        _reply(plan=PLAN, files=[BOOK], scenarios=["Извлеки"]),
        _reply(build=True),
        "не JSON", "не JSON", "не JSON", "не JSON",
    ])

    result = _dynamic(base, planner).run(deterministic_error="нет пути")

    assert result.status == "stopped"
    assert [item.filename for item in result.deliverables] == ["итоги.xlsx"]
    assert "итоги.xlsx" in result.answer and "Не удалось" not in result.answer
