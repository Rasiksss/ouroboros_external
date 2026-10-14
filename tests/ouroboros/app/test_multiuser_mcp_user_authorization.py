from __future__ import annotations

from ouroboros.app.multiuser_validation import mode_refusals
from ouroboros.storage.settings.schema import McpServerEntry
from tests.helpers.parametrize import parametrize_from_dict


@parametrize_from_dict(
    {
        "user_headers": {"auth": {"kind": "user_headers", "headers": ["X-Sudir-Token"], "cookies": ["sudir_session"]}},
        "static user_token": {
            "auth": {"kind": "user_token", "auth_header": "Authorization", "auth_token": "Bearer s"},
        },
        "personal user_token": {"auth": {"kind": "user_token", "auth_header": "X-Token"}},
        "mtls": {"auth": {"kind": "mtls"}},
        "none": {"auth": {"kind": "none"}},
    }
)
def test_no_mcp_auth_kind_is_refused_under_the_multi_user_mode(monkeypatch, auth) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("MCP_ENABLED", "true")
    entry = McpServerEntry(id="authorized-svc", url="https://authorized-svc.internal/mcp", auth=auth)
    monkeypatch.setattr("security_config.mcp.mcp_servers", [entry])

    refusals = mode_refusals()

    assert not [refusal for refusal in refusals if "authorized-svc" in refusal or "authorization" in refusal]


def test_a_forwarded_name_the_collector_does_not_declare_is_refused_under_the_multi_user_mode(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("MCP_ENABLED", "true")
    entry = McpServerEntry(
        id="authorized-svc",
        url="https://authorized-svc.internal/mcp",
        auth={"kind": "user_headers", "headers": ["X-Sudir-Token", "X-Other"]},
    )
    monkeypatch.setattr("security_config.mcp.mcp_servers", [entry])

    refusals = mode_refusals()

    assert [refusal for refusal in refusals if "authorized-svc header:x-other" in refusal]
    assert not [refusal for refusal in refusals if "x-sudir-token" in refusal]
