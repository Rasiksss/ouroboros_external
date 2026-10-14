import json
from pathlib import Path

import pytest

from finengine.skills.spec.dynamic_run import parse_decision


ROOT = Path(__file__).resolve().parents[2]
REPLAYS = json.loads((Path(__file__).with_name("fixtures") / "live_run_replays.json").read_text(encoding="utf-8"))


def test_planner_prompt_keeps_nested_json_as_values_and_schedules_new_atoms() -> None:
    prompt = (ROOT / "src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8")

    assert "`requires` и `output_schema` — JSON-массив и JSON-объект, не строки" in prompt
    assert 'НЕВЕРНО: `"requires": "[{...}]"`' in prompt
    assert 'ВЕРНО: `"requires": [{...}]`' in prompt
    assert "несколько периодов" in prompt and "не соединяй несколько периодов" in prompt
    assert "каждый атом из `new_atoms` укажи в `steps` этого же хода" in prompt


def test_planner_prompt_explains_declaring_an_atom_is_not_running_it_and_what_null_period_means() -> None:
    prompt = (ROOT / "src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8")

    assert "Объявление атома в `new_atoms` его не запускает" in prompt
    assert "НЕ ЗАПУЩЕН" in prompt
    assert "`period_ref: null` — только период самого отчёта" in prompt
    assert '"period_ref": null' not in prompt, "в примере шага период должен быть конкретным"
    assert "не финализируй" in prompt


@pytest.mark.parametrize("recording", REPLAYS, ids=[item["run"] for item in REPLAYS])
def test_recorded_model_replies_replay_without_network_or_station(recording: dict) -> None:
    decisions = [
        parse_decision(json.dumps(response, ensure_ascii=False), scenarios_enabled=True)
        for response in recording["responses"]
    ]

    assert sum(decision.action == "execute" for decision in decisions) == recording["moves"]
    terminal = decisions[-1]
    assert terminal.action == ("finalize" if recording["status"] == "finalized" else "give_up")
    if terminal.action == "finalize":
        assert terminal.evidence_ids
    else:
        assert terminal.evidence_ids == ()
