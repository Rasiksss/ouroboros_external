from __future__ import annotations

from finengine.contracts import PeerCandidate, PeerMetricValue, PeerPolicy
from finengine.skills.atoms.subject_history import select_peers
from tests.slice11_fixtures import identity
from finengine.skills.atoms.subject_history import subject_key_from_identity


def _candidate(peer_id, *, standard="ifrs", basis="IFRS", period="2025", source="report:1"):
    return PeerCandidate(
        stable_peer_id=peer_id, display_name=peer_id, company_type="bank",
        business_model="universal", standard=standard, currency="RUB",
        values=[PeerMetricValue(
            concept="ROE", value=0.2, unit="ratio", period_ref=period,
            basis=basis, source_ref=source,
        )],
    )


def test_peer_set_obeys_explicit_comparability_policy_and_sources():
    policy = PeerPolicy(
        policy_id="banks", version="1", company_type="bank", business_model="universal",
        standard="ifrs", currency="RUB", basis="IFRS", period_ref="2025",
        required_concepts=["ROE"], min_peers=2,
    )
    result = select_peers(
        subject_key=subject_key_from_identity(identity()), scenario_id="peer-scenario-1",
        policy=policy,
        candidates=[
            _candidate("peer-ok"), _candidate("peer-gaap", standard="us_gaap"),
            _candidate("peer-no-source", source=None), _candidate("peer-old", period="2024"),
        ],
    )
    assert [p.stable_peer_id for p in result.peers] == ["peer-ok"]
    assert result.sufficient is False
    reasons = {x.stable_peer_id: " ".join(x.reasons) for x in result.excluded}
    assert "standard mismatch" in reasons["peer-gaap"]
    assert "missing source" in reasons["peer-no-source"]
    assert "period mismatch" in reasons["peer-old"]
