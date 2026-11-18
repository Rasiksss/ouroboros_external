"""Remote A2A agent list for this image — the security-owned source of the list.

Mirrors `mcp.py`: editing this file is the only way to change which agents this one delegates to,
so a change goes through code review, CI and image signing. The list is built once at import time
and is fixed for the process lifetime; secrets are read here, never written here.

**Each entry declares exactly one authorization, `auth`, by `kind`** — the same egress kinds an MCP
server declares. There is no top-level `auth_header` or `auth_token`; an entry carrying one is refused
as an unknown key, and the DEV list drops it and names it in the log without its values.

- `none` (the default) — nothing is sent; an `http://` URL is allowed.
- `user_token` — `auth_header` names the header, and `auth_token` is an optional static token of the
  deployment, read through `_token(...)` below (`""` means none). Every request of a call from a
  person's surface — the send, its polls, the cancel after the deadline, and the status and cancel
  tools — carries that person's token for this agent and the calling agent when the runtime database
  holds an unexpired one, otherwise the static token, otherwise the call is refused and nothing is sent.
  Card discovery carries the static token alone, and a run that arrived through A2A ingress never looks
  up a personal token. An owner sets a personal token in owner settings.
- `user_headers` — `headers` and `cookies` name what is forwarded from the headers the browser presented
  on its WebSocket handshake: each named header as received (a header sent more than once is not
  forwarded at all), and one `Cookie` header holding the named cookies only. Only a run on the browser
  surface carries them, on every request of its call — never card discovery, never another origin.
- `mtls` — the URL addresses the deployment's egress gateway, which originates mutual TLS to the agent
  with its own certificate; the agent opens the connection as for `none`, holds no certificate and adds
  no credential. An `http://` URL is allowed.

`user_headers` and `user_token` need an `https://` URL and refuse `http://`: only `none` and `mtls` may
address a plaintext hop, so a credential and the gateway's mutual TLS cannot be combined on one entry.

**Whatever the kind, an agent is reached only at the origin of its `url`.** Its card is narrowed to the
interfaces on that scheme, host and port, and an agent left with no JSON-RPC interface there is skipped
at start; redirects are not followed and no cookie a response sets is kept. Personal tokens are stored
under the agent's `id`, so an agent moved to another host takes a new `id`.

**What a `user_headers` entry may forward is declared beside this file**, in
`auth/header_collector.yaml`, shared with `mcp.py`. With `A2A_EGRESS_ENABLED` on, the server and every
tier of the multi-user mode refuse to start when an enabled entry forwards a name the declaration does
not declare with that type, so replace the declaration together with this list.
"""

import os

from ouroboros.storage.settings.schema import A2aRemoteAgentEntry, UserTokenAuth
from ouroboros.storage.settings.store import load_settings


def _token(name: str) -> str:
    """Secret for `name` from the environment — replace the body with the team's secret engine."""
    return os.getenv(name, "")


_polygon = load_settings().OUROBOROS_POLIGON

a2a_remote_agents: list[A2aRemoteAgentEntry]

if _polygon == "DEV":
    # Legacy path: the list still comes from settings, as before this file existed.
    a2a_remote_agents = list(load_settings().A2A_REMOTE_AGENTS)
elif _polygon == "EXAMPLE":
    a2a_remote_agents = [
        A2aRemoteAgentEntry(
            id="example-agent",
            name="Example Agent",
            url="https://agent.example.internal",
            auth=UserTokenAuth(auth_header="Authorization", auth_token=_token("A2A_EXAMPLE_TOKEN")),
            featured_skills=["research"],
            retrieval_aliases=["example"],
            poll_interval_sec=2.0,
        ),
    ]
else:
    a2a_remote_agents = []
