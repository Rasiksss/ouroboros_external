"""CLI опорной записи: `record`, `compare`, `stability` (ADR-0005, F5).

Коды возврата: 0 — записано/равны, 1 — различия, 2 — ошибка предусловия (нет переменной,
чужой адрес, битый файл).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from golden_cli import main
from golden_format import DocumentRef, Meta, Recording, TurnRecord, dump, read


def write_recording(path: Path, answer: str) -> Path:
    events = (
        {
            "event_type": "legacy",
            "legacy_event": {"type": "text_delta", "text": answer},
            "sequence": 1,
        },
        {"event_type": "completed", "status": "succeeded", "sequence": 2},
    )
    recording = Recording(
        meta=Meta("S", "127.0.0.1", "b", "2026-10-16T10:00:00Z"),
        documents=(DocumentRef("a.pdf", "aa" * 32, 1, 200),),
        turns=(TurnRecord("q", events, answer, ()),),
        snapshot={"case": "нет"},
    )
    path.write_text(dump(recording), encoding="utf-8")
    return path


def test_compare_exits_zero_on_equal_recordings_and_prints_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one, two = (
        write_recording(tmp_path / "a.json", "да"),
        write_recording(tmp_path / "b.json", "да"),
    )
    assert main(["compare", str(one), str(two)]) == 0
    assert "равны" in capsys.readouterr().out


def test_compare_exits_one_and_lists_the_difference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one, two = (
        write_recording(tmp_path / "a.json", "да"),
        write_recording(tmp_path / "b.json", "нет"),
    )
    assert main(["compare", str(one), str(two)]) == 1
    assert "РАЗЛИЧИЯ" in capsys.readouterr().out


def test_compare_in_presence_mode_accepts_a_reworded_answer(tmp_path: Path) -> None:
    one, two = (
        write_recording(tmp_path / "a.json", "да"),
        write_recording(tmp_path / "b.json", "нет"),
    )
    assert main(["compare", str(one), str(two), "--text", "presence"]) == 0


def test_compare_exits_two_on_a_file_that_is_not_a_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "x.json"
    broken.write_text(json.dumps({"format": "other"}), encoding="utf-8")
    good = write_recording(tmp_path / "a.json", "да")
    assert main(["compare", str(good), str(broken)]) == 2
    assert "формат" in capsys.readouterr().err


def test_record_names_the_missing_environment_variable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "s.json"
    scenario.write_text(json.dumps({"scenario": "S", "turns": ["q"]}), encoding="utf-8")
    code = main(["record", str(scenario), "--out", str(tmp_path / "o.json")], env={})
    assert code == 2
    assert "AIFA_GOLDEN_BASE_URL" in capsys.readouterr().err


def test_record_refuses_a_foreign_address_without_touching_the_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = tmp_path / "s.json"
    scenario.write_text(json.dumps({"scenario": "S", "turns": ["q"]}), encoding="utf-8")
    env = {
        "AIFA_GOLDEN_BASE_URL": "https://example.org/api",
        "AIFA_GOLDEN_USER": "u",
        "AIFA_GOLDEN_PASSWORD": "p",
    }
    assert main(["record", str(scenario), "--out", str(tmp_path / "o.json")], env=env) == 2
    assert "не разрешён" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()


def test_a_written_recording_can_be_read_back(tmp_path: Path) -> None:
    path = write_recording(tmp_path / "a.json", "да")
    assert read(path).turns[0].answer == "да"


def test_the_report_prints_on_a_console_that_cannot_encode_the_arrow(tmp_path: Path) -> None:
    """Живой прогон на Windows: консоль cp1251 роняла отчёт на «→»."""
    one, two = (
        write_recording(tmp_path / "a.json", "да"),
        write_recording(tmp_path / "b.json", "нет"),
    )
    script = Path(__file__).with_name("golden_cli.py")
    env = {**os.environ, "PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0"}
    done = subprocess.run(
        [sys.executable, str(script), "compare", str(one), str(two)],
        env=env,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr.decode("utf-8", "replace")


def test_schemas_derives_a_file_per_event_type_from_recordings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F5 (а): `schemas` пишет `<тип>.schema.json` для каждого типа события в записях."""
    one = write_recording(tmp_path / "a.json", "да")
    two = write_recording(tmp_path / "b.json", "нет")
    out = tmp_path / "events"
    assert main(["schemas", "--out", str(out), str(one), str(two)]) == 0
    assert sorted(path.name for path in out.iterdir()) == [
        "completed.schema.json",
        "legacy.text_delta.schema.json",
    ]
    assert "2 типа" in capsys.readouterr().out


def test_schemas_exits_two_for_a_file_without_events(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    assert main(["schemas", "--out", str(tmp_path / "events"), str(other)]) == 2
