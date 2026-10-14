from __future__ import annotations

from ouroboros.app.multiuser_validation import mode_refusals
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from tests.helpers.parametrize import parametrize_from_dict


@parametrize_from_dict(
    {
        "none": {"auth": {"kind": "none"}},
        "user_headers": {"auth": {"kind": "user_headers", "headers": ["X-Sudir-Token"], "cookies": ["sudir_session"]}},
        "user_token": {"auth": {"kind": "user_token", "auth_header": "Authorization"}},
        "mtls": {"auth": {"kind": "mtls"}},
    }
)
def test_no_a2a_auth_kind_is_refused_under_the_multi_user_mode(monkeypatch, auth) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("A2A_EGRESS_ENABLED", "true")
    entry = A2aRemoteAgentEntry(id="authorized-agent", url="https://authorized-agent.internal/a2a", auth=auth)
    monkeypatch.setattr("security_config.a2a.a2a_remote_agents", [entry])

    refusals = mode_refusals()

    assert not [
        refusal
        for refusal in refusals
        if "authorized-agent" in refusal
        or "authorization" in refusal
        or "A2A egress" in refusal
        or "A2A_EGRESS_ENABLED" in refusal
    ]
