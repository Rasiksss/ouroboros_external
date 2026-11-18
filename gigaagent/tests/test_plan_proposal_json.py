import json

import pytest


@pytest.fixture
def parse():
    from ouroboros.tools.service.task_planning import _proposal_json
    return _proposal_json


def test_commentary_before_full_plan_is_accepted(parse):
    proposal = {"schema_version": "1", "steps": [{"id": "step1", "objective": "Read {text}"}]}
    message = "Проверка уровней: L1 — step1.\n\n" + json.dumps(proposal, ensure_ascii=False)
    assert json.loads(parse(message)) == proposal


@pytest.mark.parametrize("message", ['{"a": 1}', 'Text\n```json\n{"a": 1}\n```'])
def test_existing_json_and_fenced_plans_still_work(parse, message):
    assert json.loads(parse(message)) == {"a": 1}


def test_multiple_unfenced_objects_are_rejected(parse):
    with pytest.raises(ValueError):
        parse('First:\n{"a": 1}\nSecond:\n{"a": 2}')


def test_malformed_plan_is_not_repaired_from_nested_objects(parse):
    message = 'Text\n{"steps": [{"id": "step1"}], broken}'
    with pytest.raises(ValueError):
        json.loads(parse(message))
