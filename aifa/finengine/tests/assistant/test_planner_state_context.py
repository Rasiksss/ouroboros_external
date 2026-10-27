"""ADR-0096: критичный контекст планирования читается целиком при ограниченном ответе."""

from pathlib import Path
import re

from case_support import CASE, OWNER, runtime_in

from finengine.assistant import ouro_view
from finengine.contracts import GoalIR
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec import dynamic_run
from finengine.skills.spec.dynamic_run import LoopSession
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target


def session_in(tmp_path: Path, text: str) -> LoopSession:
    goal = GoalIR(raw_query="Собери Excel", intent_kind="custom_analysis")
    passport = build_run_passport(
        text,
        goal_ir_id=goal.goal_id,
        classification=classification_from_cell("ifrs_nf_ru"),
        company_hint="ПАО Проверка",
    )
    return LoopSession(
        runtime=runtime_in(tmp_path),
        owner_id=OWNER,
        case_id=CASE,
        goal=goal,
        target=Target(output_type="ReportOutput"),
        inputs=RunInputs(document_text=text),
        run_passport=passport,
        entry_stage="agent",
    )


def test_a_large_registry_has_a_lossless_continuation_instead_of_a_cut_tail(tmp_path: Path) -> None:
    session = session_in(tmp_path, "Отчёт за годы, закончившиеся 31 декабря 2025 и 2024 годов")
    state = ouro_view.state(session, limit=6000)
    assert len(state) <= 6000
    assert "registry_offset=0" in state
    assert "[обрезано]" not in state.split("## Реестр показателей\n", 1)[1].split("\n\n##", 1)[0]
    chunks = []
    offset = 0
    while offset is not None:
        reply = ouro_view.metric_registry(session, offset=offset, limit=700)
        assert len(reply) <= 700
        chunks.append(reply.split("\n\n", 1)[1].rsplit("\n\nnext_offset=", 1)[0])
        raw = re.search(r"next_offset=(\d+|null)$", reply).group(1)
        next_offset = None if raw == "null" else int(raw)
        assert next_offset is None or next_offset > offset
        offset = next_offset
    assert "".join(chunks) == dynamic_run.metric_registry_text(session)
    assert "Рассчитываемые" in "".join(chunks)
    assert "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION" in "".join(chunks)
    assert "NET_DEBT" in "".join(chunks) and "BANK_DEPOSITS" in "".join(chunks)


def test_report_identity_and_periods_are_visible_before_any_metric_is_extracted(
    tmp_path: Path,
) -> None:
    session = session_in(
        tmp_path, "Отчётность\n| Показатель | 2025 | 2024 |\n| Выручка | 100 | 90 |"
    )
    state = ouro_view.state(session, limit=12000)
    assert "ПАО Проверка" in state
    assert "2025" in state and "2024" in state
    assert "Периоды" in state


def test_unknown_periods_are_explicit_and_no_previous_year_is_invented(tmp_path: Path) -> None:
    state = ouro_view.state(session_in(tmp_path, "Финансовая отчётность без дат"), limit=12000)
    assert "периоды не определены" in state.lower()
    assert "2024" not in state and "2025" not in state


def test_dates_from_notes_do_not_become_reporting_periods(tmp_path: Path) -> None:
    text = "| Показатель | 2025 | 2024 |\n| Выручка | 100 | 90 |\nПо состоянию на 31 декабря 2013 года договор заключён."
    state = ouro_view.state(session_in(tmp_path, text), limit=12000)
    assert "2025" in state and "2024" in state
    assert "2013" not in state


def test_a_report_with_one_known_year_does_not_gain_a_comparative_year(tmp_path: Path) -> None:
    session = session_in(tmp_path, "Financial statements for FY 2025")
    state = ouro_view.state(session, limit=12000)
    assert "2025" in state and "2024" not in state


def test_values_that_look_like_years_do_not_become_periods(tmp_path: Path) -> None:
    state = ouro_view.state(
        session_in(tmp_path, "| Metric | 2025 | 2024 |\n| Revenue | 2013 | 2012 |"), limit=12000
    )
    assert "2025" in state and "2024" in state
    assert "2013" not in state and "2012" not in state


def test_a_single_period_table_header_is_visible(tmp_path: Path) -> None:
    state = ouro_view.state(
        session_in(tmp_path, "| Metric | 2025 |\n| Revenue | 100 |"), limit=12000
    )
    assert "2025" in state and "2024" not in state


def test_periods_on_the_second_header_row_are_visible(tmp_path: Path) -> None:
    text = (
        "Cover without dates\n"
        + "x" * 4100
        + "\n| Metric | Year ended | Year ended |\n| | 2025 | 2024 |\n| Revenue | 2013 | 2012 |"
    )
    state = ouro_view.state(session_in(tmp_path, text), limit=12000)
    assert "2025" in state and "2024" in state
    assert "2013" not in state and "2012" not in state


def test_ocr_headers_after_an_empty_markdown_header_are_visible(tmp_path: Path) -> None:
    text = "| | | | |\n| --- | --- | --- | --- |\n| | Примечание | 31 декабря 2025 | 31 декабря 2024 |\n| Выручка | | 2013 | 2012 |"
    state = ouro_view.state(session_in(tmp_path, text), limit=12000)
    assert "2025" in state and "2024" in state
    assert "2013" not in state and "2012" not in state


def test_a_later_note_table_does_not_add_dates_to_the_main_report_periods(tmp_path: Path) -> None:
    text = "| Metric | 2025 | 2024 |\n| Revenue | 100 | 90 |\n\nNotes\n| Contract | 2013 | 2012 |\n| A | 1 | 2 |"
    state = ouro_view.state(session_in(tmp_path, text), limit=12000)
    assert "2025" in state and "2024" in state
    assert "2013" not in state and "2012" not in state
