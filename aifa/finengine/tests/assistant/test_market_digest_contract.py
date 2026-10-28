"""ADR-0098: рыночная часть цели не требует финансового отчёта."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from finengine.contracts.digest import DigestOutput, DigestParameters
from finengine.skills.spec.agent_goals import goal_clause


def test_market_goal_has_typed_parameters():
    clause = goal_clause(
        {"ref": "g1", "scenario": "market_digest", "parameters": {"since_hours": 168}},
        where="goals[0]", raw_query="дайджест за неделю",
    )
    assert clause.intent_kind == "market_digest"
    assert clause.parameters == {"since_hours": 168}
    assert not clause.targets


@pytest.mark.parametrize("value", [True, "168", 5, 337])
def test_market_window_is_strict_and_bounded(value):
    with pytest.raises(ValidationError):
        DigestParameters(since_hours=value)


def test_market_parameters_do_not_accept_identity_or_urls():
    with pytest.raises(ValidationError):
        DigestParameters(user_id=1, url="http://localhost")


def test_market_goal_rejects_report_fields():
    with pytest.raises(ValueError, match="отчёт"):
        goal_clause(
            {"ref": "g1", "scenario": "market_digest", "concepts": ["SALES_REVENUE"]},
            where="goals[0]", raw_query="дайджест",
        )


def test_digest_snapshot_round_trips_with_its_original_timestamp():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    digest = DigestOutput(
        window_start=now, window_end=now, collected_at=now, markdown="# Выпуск",
        status="empty", source_stats={"total": 1, "failed": 0},
    )
    restored = DigestOutput.model_validate_json(digest.model_dump_json())
    assert restored == digest
    with pytest.raises(ValidationError):
        digest.markdown = "Подменённый выпуск"
