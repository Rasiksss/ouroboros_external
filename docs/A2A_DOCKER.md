# A2A in Docker (multi-instance)

Ouroboros implements the [Agent-to-Agent (A2A)](https://google.github.io/A2A/) protocol so agents can call each other over HTTP JSON-RPC.

## Ports

| Port | Service |
|------|---------|
| 8080 | Headless health/state API (`Dockerfile` default) |
| 18800 | A2A server (Agent Card + JSON-RPC), when `A2A_ENABLED=true` |

A2A is **off** in the base image; enable via env (see below) or `docker-compose.a2a.yml`.

Runtime dependency: `a2a-sdk[http-server]==1.0.3` ([a2a-python](https://github.com/a2aproject/a2a-python)).

## Enable A2A

**Docker / env** (recommended for containers):

```bash
A2A_ENABLED=true
A2A_HOST=0.0.0.0
A2A_PORT=18800
A2A_PUBLIC_URL=http://my-service:18800   # hostname other agents use
```

**Configuration:** set environment variables (see table above) and restart the server. The headless tree has no Settings UI.

`A2A_PUBLIC_URL` overrides the URL in `/.well-known/agent-card.json`. Set it to the address reachable from other containers (e.g. Docker Compose service name), not `0.0.0.0`.

## Incoming authentication

**There is none.** The A2A endpoint accepts any request that reaches it: anyone who can open
port 18800 can queue work on this agent, and the JSON-RPC route is as open as the agent card.

Keep the listener off the public network — loopback, or the same ingress/TLS/VPN controls the
main server sits behind. `A2A_HOST` defaults to `127.0.0.1` for that reason; `0.0.0.0` inside
a container is only safe while the port is published to the local machine, as
`docker-compose.yml` does.

## Calling other agents (outbound)

Outbound A2A is off by default, and the flag is the only half of it that lives in the
environment:

```bash
A2A_EGRESS_ENABLED=true
A2A_TOOL_TIMEOUT_SEC=300
```

The agents to reach come from `security_config/a2a.py`, which the server imports once at start.
Add a branch there for the polygon this instance runs as (`OUROBOROS_POLIGON`, a free-form
string) and build the entries in code, with tokens read from the environment:

```python
a2a_remote_agents = [
    A2aRemoteAgentEntry(
        id="researcher",
        name="Research Agent",
        url="http://agent-b:18800",
        auth_header="Authorization",
        auth_token=_token("A2A_RESEARCHER_TOKEN"),
        retrieval_aliases=["research"],
        poll_interval_sec=2.0,
    ),
]
```

For the `DEV` polygon the module delegates to the legacy `A2A_REMOTE_AGENTS` setting instead, so
a local run can still pass the list as JSON in the environment:

```bash
OUROBOROS_POLIGON=DEV
A2A_REMOTE_AGENTS='[{"id":"researcher","name":"Research Agent","url":"http://agent-b:18800","auth_header":"Authorization","auth_token":"Bearer REPLACE_ME","featured_skills":[],"retrieval_aliases":["research"],"tool_result_limit":30000,"poll_interval_sec":2}]'
```

Each enabled entry becomes exactly one tool, `a2a__{id}`, plus the two process-wide
`a2a_task_status` / `a2a_task_cancel` tools — N agents cost N+2 tool names. `auth_header` and
`auth_token` are the credential the *other* agent asks callers for; both must be set or
neither is sent. `featured_skills` selects which of the remote agent's published skills
appear in the tool description and gates nothing — the protocol has no way to address a skill.

- `A2A_EGRESS_ENABLED` is read once at startup and handed to the outbound manager: changing it
  takes effect on the next restart. With the flag on and the list empty, the manager is not
  initialized at all and the log says so.
- `A2A_REMOTE_AGENTS` is the **legacy** source, kept for the `DEV` polygon and unchanged in
  `AppSettings`. It is **never** projected into a subprocess environment, because it carries
  credentials. Set it on the agent process itself.
- An entry that fails validation is dropped and the rest are loaded; an agent that cannot be
  reached at startup is logged and skipped rather than failing the boot.
- The delegation *behaviour* — when to delegate, how to handle long work — ships as the
  `agent-delegation` instruction skill. It appears in the skill catalogue exactly while
  `A2A_EGRESS_ENABLED` is on, and is found from the tree that ships the code, so it does not
  depend on `OUROBOROS_REPO_DIR` pointing at that tree.

## Two-agent Compose example

```bash
docker build --platform linux/amd64 -t ouroboros-headless .
cp .env.a2a.example .env
# Edit .env: set either GIGACHAT_B2BANK_* or CLOUDRU_FOUNDATION_MODELS_API_KEY

docker compose -f docker-compose.a2a.yml up
./scripts/a2a_smoke.sh
```

- Agent A health: http://localhost:8080 — A2A: http://localhost:18800
- Agent B health: http://localhost:8081 — A2A: http://localhost:18801

Inside the network, agents reach each other as `http://agent-a:18800` and `http://agent-b:18800`.

### A2A Inspector (docker-compose.yml)

The Inspector **backend** runs inside Docker. In the UI at http://localhost:8088:

1. **Agent URL:** `http://agent:18800` (service name on the compose network). Do **not** use `http://localhost:18800` — from the Inspector container that points at itself, not your agent.
2. Click **Connect** and wait for a successful agent card / `client_initialized` before chatting. Sending a message without Connect returns `{"error": "Client not initialized."}`.
3. **LLM env:** set `CLOUDRU_FOUNDATION_MODELS_API_KEY` (or `GIGACHAT_B2BANK_*`) in `.env`. Plain model IDs such as `OUROBOROS_MODEL=zai-org/GLM-5.1` are valid for Cloud.ru Foundation API — the supervisor will start with the API key alone.

If chat returns `Supervisor not ready — bridge not initialized`, the process marked itself ready without a running supervisor (usually missing/invalid provider env). Check `docker logs` for `Supervisor ready` vs `Supervisor init failed`.

## Startup order

The headless server starts the **supervisor** in a background thread (LLM provider required). The **A2A** listener on port 18800 starts only after the supervisor bridge is ready (or after `OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC`, default 300s).

`GET /health/readiness` returns **503** until the supervisor is up; `docker-compose.yml` uses this for the agent healthcheck so the A2A Inspector starts after the agent can accept tasks.

## Smoke test

```bash
chmod +x scripts/a2a_smoke.sh
./scripts/a2a_smoke.sh
# Full round-trip (needs a provider in .env):
SEND_MESSAGE=1 ./scripts/a2a_smoke.sh
```

Manual curl:

```bash
curl -s http://localhost:18800/.well-known/agent-card.json | jq .url
```

## Related files

- Server: `ouroboros/ingress/a2a_server.py`, `ouroboros/ingress/a2a_executor.py`
- Client: `ouroboros/egress/a2a/` (transports, connection, manager), `new_core/tools/providers/a2a.py`
- Agent list: `security_config/a2a.py` (per-polygon, imported at startup)
- Delegation skill: `skills/agent-delegation/SKILL.md`
- Architecture: [docs/ARCHITECTURE.md](ARCHITECTURE.md) (ingress + optional A2A)
- Deployment: [docs/DEPLOYMENT.md](DEPLOYMENT.md)
