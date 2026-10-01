# DEPLOYMENT.md — Headless production deployment

This runbook covers the **Docker / VM headless profile** in this repository. For the full production target (SberChat edge, Secman, sandbox, audit export), see the wiki: [Solution Architecture](https://gitlab.k8s.genai.int/gen-ai/fusion_team/backend/ouroboros/-/wikis/Solution-Архитектура-As-Is).

---

## Quick start (Docker)

**Local laptop** (PyPI inside the build; no Nexus CA secret):

```bash
docker build -f Dockerfile.local --platform linux/amd64 -t ouroboros-headless .
```

**CI / corporate registry** (`Dockerfile`, frozen `uv.lock`, Nexus CA):

```bash
docker build --platform linux/amd64 \
  --secret id=nexus_ca,src=/path/to/nexus-ca.crt \
  -t ouroboros-headless .
```

```bash
# run (either image)
docker run --rm -p 8080:8080 \
  -e OUROBOROS_DATA_DIR=/data \
  -v ouroboros-data:/data \
  -e CLOUDRU_FOUNDATION_MODELS_API_KEY='...' \
  -e OUROBOROS_MODEL='cloudru::zai-org/GLM-4.7' \
  -e DIALOGS_GRPC_ENDPOINT='...' \
  -e DIALOGS_BOT_TOKEN='...' \
  ouroboros-headless
```

GigaChat B2Bank alternative:

```bash
docker run --rm -p 8080:8080 \
  -e OUROBOROS_DATA_DIR=/data \
  -v ouroboros-data:/data \
  -e GIGACHAT_B2BANK_BASE_URL='https://…' \
  -e GIGACHAT_B2BANK_CERT_PATH=/run/secrets/gigachat.crt \
  -e GIGACHAT_B2BANK_KEY_PATH=/run/secrets/gigachat.key \
  -e DIALOGS_GRPC_ENDPOINT='...' \
  -e DIALOGS_BOT_TOKEN='...' \
  ouroboros-headless
```

The image sets by default (`Dockerfile`):

| Variable | Default |
|----------|---------|
| `OUROBOROS_ENV` | `production` |
| `OUROBOROS_SERVER_HOST` | `0.0.0.0` |
| `OUROBOROS_SERVER_PORT` | `8080` |
| `OUROBOROS_DATA_DIR` | `/data` |
| Process user | `ouroboros` (UID 10001) |

**Entrypoint:** `python server.py --host 0.0.0.0 --port 8080`

---

## Required configuration

### Network exposure

The headless HTTP surface is health-only. **User authentication is not implemented in-process** — protect non-loopback binds with ingress auth, TLS, VPN, or a private network (wiki: SberChat / approved identity gateway).

Public paths: `/health/liveness`, `/health/readiness`, `/api/health`.

### LLM providers

Configure at least one supported provider. If `CLOUDRU_FOUNDATION_MODELS_API_KEY` is set and
GigaChat credentials are absent (and `SELFHOSTED_LLM_BASE_URL` is not set), the runtime
auto-selects Cloud.ru defaults. Explicit `OUROBOROS_MODEL*` values override those defaults.

When `SELFHOSTED_LLM_BASE_URL` is set, model slots use **plain model IDs** (no `provider::`
prefix) for the self-hosted endpoint. Slots with an explicit prefix (`cloudru::…`,
`gigachat-b2bank::…`) still route to that provider.

#### Self-hosted OpenAI-compatible LLM

| Variable | Purpose |
|----------|---------|
| `SELFHOSTED_LLM_BASE_URL` | OpenAI-compatible API host (with or without `/v1`; runtime appends `/v1` when missing) |
| `SELFHOSTED_LLM_API_KEY` | Optional Bearer key (many servers accept any non-empty value) |
| `OUROBOROS_MODEL` | Plain model ID, e.g. `my-llama-3` (no prefix) |
| `OUROBOROS_LLM_HTTP_READ_TIMEOUT_SEC` | Optional read timeout for remote LLM HTTP (default `300`) |
| `OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC` | Background read override; distillation separately caps every HTTP phase at 30 seconds and its remaining 45-second job deadline; consolidation bounds one run (primary + fallback) by this window plus a 60-second margin (default `300`) |
| `OUROBOROS_LLM_HTTP_CONNECT_TIMEOUT_SEC` | Optional connect timeout (default `30`) |

ModelRun / cold endpoints can take tens of seconds for the first token; without a read timeout the A2A task stays in `working` until `OUROBOROS_HARD_TIMEOUT_SEC`.

Example mixed routing:

| Slot | Value | Routes to |
|------|-------|-----------|
| `OUROBOROS_MODEL` | `my-llama-3` | Self-hosted |
| `OUROBOROS_MODEL_LIGHT` | `cloudru::zai-org/GLM-4.7` | Cloud.ru |
| `OUROBOROS_MODEL_VISION` | `cloudru::Qwen2.5-VL` | Cloud.ru |

`OUROBOROS_MODEL_VISION` is the dedicated vision-capable model used by the `vlm_query`
tool for image analysis. Set it when the main model is text-only; when empty, the tool
falls back to `OUROBOROS_MODEL` and assumes it accepts images.

#### Cloud.ru Foundation Models

| Variable | Purpose |
|----------|---------|
| `CLOUDRU_FOUNDATION_MODELS_API_KEY` | Bearer API key |
| `CLOUDRU_FOUNDATION_MODELS_BASE_URL` | Optional, defaults to `https://foundation-models.api.cloud.ru/v1` |
| `OUROBOROS_MODEL` | e.g. `cloudru::zai-org/GLM-4.7` |

#### GigaChat B2Bank

| Variable | Purpose |
|----------|---------|
| `GIGACHAT_B2BANK_BASE_URL` | API base |
| `GIGACHAT_B2BANK_CERT_PATH` | Optional client certificate; must be paired with the private key |
| `GIGACHAT_B2BANK_KEY_PATH` | Optional private key; must be paired with the client certificate |
| `GIGACHAT_B2BANK_CA_PATH` | Optional CA bundle |
| `GIGACHAT_B2BANK_VERIFY_SSL` | `true` / `false`; controls server certificate verification independently of the optional client cert/key |
| `GIGACHAT_B2BANK_CHAT_API_VERSION` | Chat Completions wire contract: `v1` (default) or non-stream `v2`; file uploads always use `/v1/files` |
| `GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS` | `true` / `false` (default); v2 only — allows the model to emit several tool calls per round. Once enabled, downgrading `CHAT_API_VERSION` to `v1` breaks replay of conversations that already contain multi-call rounds (v1 cannot represent them) |
| `OUROBOROS_MODEL` | e.g. `gigachat-b2bank::GigaChat-Pro` |

The configured chat version overrides a trailing `/v1` or `/v2` in `GIGACHAT_B2BANK_BASE_URL`.
Changing it switches all GigaChat B2Bank chat requests in the process; mixed v1/v2 routing is not supported.

### GigaSearch

`gigasearch` is a core read-only tool and is present in the initial LLM tool surface.
It calls GigaSearch v3 in advanced mode across all available sources.

| Variable | Purpose |
|----------|---------|
| `GIGASEARCH_BASE_URL` | API base; defaults to `https://gigachat.devices.sberbank.ru` |
| `GIGASEARCH_USER_ID` | Value of the `X-User-ID` request header; may be empty |
| `GIGASEARCH_SURFACE_ID` | Value of the `X-Surface-ID` request header; may be empty |
| `GIGASEARCH_API_KEY` | Reserved secret setting kept for deployment compatibility; the current v3 contract does not send it |
| `GIGASEARCH_TIMEOUT_SEC` | Per-request HTTP timeout; defaults to `30` when unset or zero |
| `GIGASEARCH_CERT_PATH` | Optional client certificate path; must be paired with the private key |
| `GIGASEARCH_KEY_PATH` | Optional private key path; must be paired with the client certificate |
| `GIGASEARCH_CA_PATH` | Optional CA bundle used when server verification is enabled |
| `GIGASEARCH_VERIFY_SSL` | Server certificate verification, default `true`; when `false`, CA path is ignored |

Certificate paths are intended for local/integration debugging and mounted runtime
secrets. Do not commit certificate or private-key material.

Every search query is checked against the Dozor Mailfilter DLP service before the
search call; a blocked or failed check returns a tool error without calling the
search API (fail-closed).

| Variable | Purpose |
|----------|---------|
| `DLP_BASE_URL` | Mailfilter endpoint issued by the DLP team, e.g. `http://<dlp-host>:3010`; empty (the default) disables the DLP check |
| `DLP_MAPPING_SCHEMA` | `X-Dozor-Mapping-Schema` and `X-Application` header value agreed with the DLP team, default `orion` |
| `DLP_SENDER_EMAIL` | `X-Sender-User-Email` header, technical account address; empty by default |
| `DLP_RECIPIENT_EMAIL` | `X-Recipient-User-Email` header |
| `DLP_SERVER_IP` | `X-Server-IP` header, destination IP (stub `1.1.1.1` until the real address is agreed) |
| `DLP_CLIENT_IP` | `X-Client-IP` header, application host IP (stub `1.1.1.1` until the real address is agreed) |
| `DLP_SUBJECT` | `X-Subject` header as plain text (Base64-encoded by the client), default `Поиск в интернете` |
| `DLP_CONTENT_TYPE` | `Content-Type` of the checked content, default `application/octet-stream` |
| `DLP_ALLOW` | `Allow` header, default `204`; with it Dozor replies `204` for an ok verdict and `403` for a blocked query instead of the JSON report |
| `DLP_TIMEOUT_SEC` | DLP request timeout in seconds, default `30` |

Headers with an empty configured value are omitted from the request;
`X-Message-Date` and `X-Message-ID` are set by the client itself.

### Dialogs / SberChat ingress

| Variable | Purpose |
|----------|---------|
| `DIALOGS_GRPC_ENDPOINT` | gRPC endpoint for Dialogs provider |
| `DIALOGS_BOT_TOKEN` | Bot token |
| `DIALOGS_GROUP_ID` | Group scope (when used) |
| `DIALOGS_USER_ID` | Optional allowed sender id within the accepted chat/group |
| `DIALOGS_SECURE` | TLS enabled |
| `DIALOGS_FILE_URL_PORT` | HTTP port used by SDK file upload/download URL rewriting when `DIALOGS_SECURE=false`; default `80` |
| `DIALOGS_ROOT_CERTIFICATES` | Optional trust roots |
| `DIALOGS_REPLY_ENABLED` | Use `reply_sync` for responses when the inbound message id is available; default `false` |
| `DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED` | Update one progress message with `update_message_sync`; default `false` |
| `DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED` | Pass the inbound sender as `uid` for group-targeted output; default `false` |
| `DIALOGS_HISTORY_RECOVERY_ENABLED` | Import bounded SDK message history once per peer (a configured group at startup, any other peer on its first live message) and again after an updates-stream outage, without starting old agent tasks; default `false` |
| `DIALOGS_HISTORY_RECOVERY_LIMIT` | Maximum SDK history items per recovered peer, validated from 1 to 100; default `10` |
| `DIALOGS_FORWARDING_DISABLED` | Set `is_forward_banned=True` on outbound messages/files; default `false` |
| `DIALOGS_TYPING_ENABLED` | Enable Presence typing: started when a task begins, re-sent before every LLM request, stopped when the task ends; default `false` |
| `DIALOGS_PARTICIPANT_IDENTITY_ENABLED` | Resolve the sender name through the SDK and label stored messages with it; default `false` |
| `DIALOGS_BOT_NICK` | Bot nick (with or without `@`). When set, a live group message is handled only if it mentions the nick anywhere in the text (`@bot, текст`, `посмотри @bot логи`), is a runtime slash command (`/status`, `/restart`, `/stop`), or replies to a message the bot sent. Clearing the history is not admitted unaddressed, because its vocabulary is ordinary speech and it wipes all stored messages, not just one chat. Everything else in the group is still stored as shared context without starting a task; attachments in such messages are recorded by name and not downloaded. Empty disables the gate |

Without these, the HTTP server may start but **no user messages** will arrive.

### Scheduler storage

The bundled `tasks-scheduler` extension stores its state through the application's
configured `StorageProvider`; it has no separate storage settings or credentials.

`send_assignment` is provided by the bundled `assignment-delegation` extension skill (`skills/assignment-delegation/`); the skill must be present and live-loaded for assignment delivery to be available. SMTP and the outbound-only Dialogs bot are independent delivery channels: configure either one or both. A selected SMTP channel requires host, username, password, sender, and recipients; a selected Dialogs channel requires `DIALOGS_GRPC_ENDPOINT`, `ASSISTANT_DIALOGS_BOT_TOKEN`, and `ASSISTANT_DIALOGS_BOT_GROUP_ID`. Partial configuration of either selected channel fails at settings load. With neither channel selected the application can start, but assignment delivery returns a configuration error without network I/O. SMTP uses `STARTTLS`, optional trust-root/client-certificate setup, and username/password auth. Leave `ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE` unset for ordinary TLS; set it to enable SMTP mTLS.

Each send returns an `assignment_id` but does not persist assignment delivery state under `state/assignment_delivery/`; channel-specific retry by `assignment_id` is intentionally unavailable. When both channels are selected, SMTP and SberChat delivery start in parallel. SMTP retries and SberChat waiting are bounded by the tool's internal delivery deadline, so exhausted time returns `deadline_exceeded` instead of waiting for the full tool timeout. Only `status=sent` confirms that every selected channel delivered; failed and partial results are surfaced as tool errors. A partial result tells the agent in plain language that some targets may already have received the assignment, so it must report partial delivery and not retry automatically.

Delivery diagnostics are emitted through the logger of `skills/assignment-delegation/delivery.py` (loaded under the extension module namespace) to pod stdout, and as structured tool events to the metadata DB `audit_events` (`log_stream=tools`). Events include channel start/finish, SMTP attempt start/finish, SMTP code, status, duration, assignment id, and task id when available; secrets, assignment text, and assignment text hashes are not logged. The extension declares `subject` and `body` as log-redacted arguments, so generic tool-call argument logs omit them entirely instead of hashing them. The declared redaction set is snapshotted once when the tool call is resolved and reused for every log in that execution; if extension metadata is unavailable at that point (e.g. the extension was unloaded), all arguments are omitted fail-closed.

Scheduler objects live under its private `state/skills/tasks-scheduler/` namespace.
Conditional writes preserve revision checks and at-most-once slot claims on both
file and S3 backends.

### Optional browser ingress

Set `BROWSER_INGRESS_ENABLED=true` to mount `/api/state`, shared chat history,
upload/download/delete endpoints, and WebSocket `/ws`. Authentication is expected at
the external gateway. Uploaded files use `OUROBOROS_USER_FILE_TTL_HOURS` (default 720,
or 30 days).

### Self-hosted LLM TLS

Self-hosted OpenAI-compatible providers can use ordinary TLS, custom CA trust, or client certificate authentication.

| Variable | Purpose |
|----------|---------|
| `SELFHOSTED_LLM_BASE_URL` | OpenAI-compatible base URL; `/v1` is appended when omitted |
| `SELFHOSTED_LLM_API_KEY` | Optional API key; empty means no Authorization header |
| `SELFHOSTED_LLM_CERT_PATH` | Optional client certificate path; must be paired with key |
| `SELFHOSTED_LLM_KEY_PATH` | Optional client key path; must be paired with cert |
| `SELFHOSTED_LLM_CA_PATH` | Optional CA bundle path when verification is enabled |
| `SELFHOSTED_LLM_VERIFY_SSL` | Server certificate verification, default `true`; when `false`, CA path is ignored |

### Budget and executor

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_SOFT_TIMEOUT_SEC` | `600` | Warning |
| `OUROBOROS_HARD_TIMEOUT_SEC` | `1800` | Kill |
| `OUROBOROS_CHECKPOINT_PROGRESS_ENABLED` | `false` | Send periodic checkpoint summaries as user-facing progress messages; self-check prompts and structured `task_checkpoint` events remain enabled |

Tasks run on a **single in-process executor** (`supervisor/workers.py`, `max_workers=1`); there is no multi-worker pool env var in headless production.

Wiki target: **hard-stop before each LLM/tool spend** — treat current budget as operational but verify behavior before production pilot.

### Identity self-edit

**There is no variable for this — the switch is hardcoded off.**
`identity_self_edit_enabled()` in
`ouroboros/storage/core_memory/memory/domain/models.py` returns `False` unconditionally,
so no environment variable, settings file, or tool call can open the identity document
for rewriting. The former `OUROBOROS_IDENTITY_SELF_EDIT_ENABLED` setting is gone; if a
deployment still exports it, it is inert.

Deliberately so: the identity document is the agent's continuity across every task, and
it is injected into every prompt under `## Identity`. Enabling self-edit would accept the
residual risk that untrusted content reaching the agent argues it into a rewrite, and a
rewrite is not reversible through the tools — identity is exactly one row, the replaced
text is not archived, and recovery would mean restoring the runtime database from backup.

Operationally this means: the `identity` option is absent from the `memory_save` kind
enum, the `## Identity` prompt header states the document is read-only, and the domain
refuses the write. Deletion was impossible at either switch value and remains so.
Distillation was never offered identity at either value.

The write path itself stays in the codebase behind that switch (name exactly `identity`,
`mode='overwrite'`, non-empty, at most 20 000 characters, plus an `identity rewritten`
log record carrying sizes and a body hash prefix but never the body). Flipping the
constant is a code change and a deliberate decision, not a deployment knob.

To read the stored document — for a backup copy, or to check what the prompt is carrying:

```bash
uv run python -c "from ouroboros.storage.core_memory.memory import get_memory; print(get_memory().fetch('identity').content)"
```

Writing one back is a database operation: `get_memory().save('identity', ...)` is refused
by the domain like every other identity write while the switch is off.

### External servers (MCP and A2A egress)

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_POLIGON` | `DEV` | Free-form polygon name (`DEV`, `PROM`, `IFT`, … — no validation) picking the branch `security_config/mcp.py` and `security_config/a2a.py` build their lists from |
| `MCP_ENABLED` | `false` | Kill switch above the MCP list; forced off by the production overlay |
| `A2A_EGRESS_ENABLED` | `false` | Kill switch above the remote-agent list |

Which servers this instance talks to is part of the **image**, not of the environment:
`server.py` imports `mcp_servers` from `security_config/mcp.py` and `a2a_remote_agents` from
`security_config/a2a.py` once at start, so changing a list is a code change with review, CI and
signing behind it, and it takes effect on the next start. An empty list for the current polygon
means the manager is not initialized at all — the flags above still gate it, but they cannot add
a server. Tokens are read at import time from the environment (`_token("MCP_EXAMPLE_TOKEN")` in
the shipped example) or from whatever secret engine the team calls there; never write a secret
into the file itself.

Three ways to configure a polygon, in descending order of preference:

1. **Edit the file and rebuild.** Add a branch for the polygon in `security_config/mcp.py` /
   `security_config/a2a.py`, commit, rebuild the image. This is the reviewed path.
2. **Mount the file over the image's copy** (`-v ./mcp.py:/app/security_config/mcp.py:ro`, or a
   ConfigMap in Kubernetes) when a polygon needs its own list without its own image. The content
   then bypasses image review, so it needs the same scrutiny somewhere else.
3. **Insert a secret engine** — replace the body of `_token()` with the team's client. It runs at
   import, before the server binds its port, so give it a timeout: a hanging call hangs startup.

A polygon with no branch of its own gets an empty list, so nothing is connected and the manager is
not initialized. The `EXAMPLE` polygon is the shipped template of a filled-in branch: set
`OUROBOROS_POLIGON=EXAMPLE` to see what one looks like end to end (its URL does not resolve, so the
log shows `Skipping MCP server example-mcp after connection failure`), then copy it into a branch
of your own.

### Runtime metadata database

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_RUNTIME_DB_URL` | *(empty)* | Single-node PostgreSQL URL, or dev fallback when numbered URLs are unset. When unset entirely, SQLite at `{OUROBOROS_DATA_DIR}/state/runtime.sqlite3`. |
| `OUROBOROS_RUNTIME_DB_URL_1` … `_N` | *(unset)* | Full PostgreSQL connection strings for HA failover (each with its own host, SSL, schema `options`, timeouts, etc.). Contiguous from `_1`; first gap stops collection. |
| `OUROBOROS_RUNTIME_DB_URL_MAX` | `16` | Upper bound on numbered URLs scanned (hard cap 64). |

Percent-encode reserved URL characters in the userinfo segment (password, and username if needed). Example: password `p%a` → `postgresql+psycopg://user:p%25a@host:5432/ouroboros`. The runtime normalizes each URL for SQLAlchemy/psycopg and adds `target_session_attrs=read-write` when not set explicitly (connect to current primary). Alembic config escaping is handled internally.

**HA example** (two nodes, same credentials/schema options, different hosts):

```bash
export OUROBOROS_RUNTIME_DB_URL_1='postgresql+psycopg://user:pass@pg1:5432/ouroboros?sslmode=require&options=-csearch_path%3Dmyschema'
export OUROBOROS_RUNTIME_DB_URL_2='postgresql+psycopg://user:pass@pg2:5432/ouroboros?sslmode=require&options=-csearch_path%3Dmyschema'
```

When `_1` is set, legacy `OUROBOROS_RUNTIME_DB_URL` is ignored. With a single node, `OUROBOROS_RUNTIME_DB_URL` alone is unchanged.

On startup the process runs Alembic `upgrade head` against the configured URL(s). For ops-only migration before start:

```bash
export OUROBOROS_RUNTIME_DB_URL='postgresql+psycopg://…'
# or export OUROBOROS_RUNTIME_DB_URL_1=… OUROBOROS_RUNTIME_DB_URL_2=…
uv run alembic -c alembic.ini upgrade head
```

Major schema changes require a **fresh** database or volume (no in-place upgrade from pre-Alembic `runtime.sqlite3` files).

---

## Persistence without PVC (production)

Production pods should use **no PVC** for agent data:

1. **PostgreSQL** — required (`OUROBOROS_RUNTIME_DB_URL` or `OUROBOROS_RUNTIME_DB_URL_1..N`). Do not rely on `state/runtime.sqlite3` on ephemeral disk.
2. **S3-compatible object storage** — required for durable files (memory, private extension state, uploads, A2A task files). Owner runtime state is in PostgreSQL (`owner_runtime_state`), not S3. **Structured runtime logs are not stored in S3** (see Filebeat below).

| Variable | Purpose |
|----------|---------|
| `OUROBOROS_RUNTIME_DB_URL` / `_1..N` | `postgresql+psycopg://…` (required in production; use numbered URLs for HA failover) |
| `OUROBOROS_STORAGE_BACKEND` | `s3` (default when `OUROBOROS_ENV=production`) |
| `OUROBOROS_S3_BUCKET` | Target bucket |
| `OUROBOROS_S3_ENDPOINT` | Optional MinIO / corporate S3 endpoint |
| `OUROBOROS_S3_REGION` | Region |
| `OUROBOROS_S3_ACCESS_KEY` / `OUROBOROS_S3_SECRET_KEY` | Credentials (or IAM role on node) |
| `OUROBOROS_S3_PREFIX` | Optional key prefix inside bucket |
| `OUROBOROS_STORAGE_TENANT_ID` | Key segment (default `default`) |
| `OUROBOROS_ALLOW_FILE_STORAGE_IN_PRODUCTION` | `true` only for exceptional local prod tests |

`OUROBOROS_DATA_DIR` is a **writable emptyDir** (or small mount): locks, temp paths, and **memory `*_journal.jsonl`**. Python stdlib logs (`ouroboros.observability.logging_setup`) go to pod stdout only and are never written under `logs/`. All runtime event streams (`log_stream=events|tools|supervisor`) are stored in the metadata DB `audit_events`; no runtime stream writes `logs/*.jsonl` anymore. When `OUROBOROS_STORAGE_BACKEND=s3`, `ScopedHybridProvider` routes `memory/` (except journals), `state/`, `uploads/`, and `a2a_tasks/` to S3; `logs/` always stays on disk for append and Filebeat tail. Stale files left on the pod disk from an earlier file-backend deploy are **not** read (`stale_local_shadow` log line); migrate data into the bucket before switching backends.

### Filebeat / log shipping

Runtime event streams (`events`, `tools`, `supervisor`) are no longer pod-local JSONL files; query/export the metadata DB `audit_events` (filter by `log_stream`). Python stdlib logs are **not** part of this file-based set — they go to stdout only and should be collected by the cluster's stdout/container-log pipeline (e.g. a node-level log agent), not Filebeat.

| Path | Content |
|------|---------|
| metadata DB `audit_events` | Runtime event streams (`log_stream=events|tools|supervisor`) |
| pod stdout | Python stdlib logs (`OUROBOROS_LOG_FORMAT=json`), collected via container stdout, not a file |

Long-term retention and search live in Elasticsearch/Loki (or your SIEM), not in the S3 bucket. Pod-local logs are ephemeral; ship before restart.

Logging knobs:

- `OUROBOROS_LOG_FORMAT=json|text` (default `json`)
- `OUROBOROS_LOG_LEVEL=INFO|WARNING|...` (default `INFO`)

**Backup:** PostgreSQL dump + S3 bucket lifecycle/replication for memory/state; log backup via Filebeat destination, not S3 object keys under `logs/`.

**Development:** `OUROBOROS_ENV=development` defaults to `file` storage under `OUROBOROS_DATA_DIR` and SQLite runtime DB.

**Encryption:** bucket encryption at rest (cloud); TLS in transit to S3 endpoint.

---

## Vault secret files (Kubernetes)

When Vault (or another sidecar) mounts secrets as **one file per env key**, set the mount directory. Files are merged at startup in `bootstrap_config()` before the in-process config singleton is built.

| Variable | Purpose |
|----------|---------|
| `OUROBOROS_SECRETS_DIR` | Directory with secret files (for example `/vault/secrets`) |
| `{OUROBOROS_SECRETS_DIR}/{KEY}` | File contents override OS env for that `KEY` at bootstrap (filename case-insensitive) |
| `OUROBOROS_SECRETS_WAIT_SEC` | Max seconds to poll for the mount directory and secret files (default `30`; `0` disables wait) |
| `OUROBOROS_SECRETS_WAIT_INTERVAL_SEC` | Poll interval in seconds (default `0.5`) |

**Mount wait:** when `OUROBOROS_SECRETS_DIR` is set, `bootstrap_config()` polls for the full wait budget: first until the path is a directory, then keeps scanning for new files (Vault sidecars often mount one file at a time). Polling does not stop after the first file appears. On timeout the process logs a warning if the directory never appeared and continues bootstrap with whatever files were collected.

**Priority:** secret file **overrides** OS env for the same key (env fills gaps when no file exists).

Example layout:

```text
OUROBOROS_SECRETS_DIR=/vault/secrets
/vault/secrets/dialogs_bot_token
/vault/secrets/dialogs_grpc_endpoint
/vault/secrets/gigachat_b2bank_cert_path
/vault/secrets/selfhosted_llm_cert_path
/vault/secrets/selfhosted_llm_key_path
```

Implementation: `ouroboros/storage/settings/env_files.py` (`load_secret_files`) + `ouroboros/storage/settings/bootstrap.py` (`bootstrap_config`).

---

## Paths

| Variable | Role |
|----------|------|
| `OUROBOROS_DATA_DIR` | Writable data root |
| `OUROBOROS_REPO_DIR` | Read-only repo root (`prompts/`, `docs/`, `skills/` seeds) |

---

## Optional: A2A

See [A2A_DOCKER.md](A2A_DOCKER.md). Requires `A2A_ENABLED=true` and a separate port; protect non-loopback exposure at the network edge.

---

## Health checks

| Endpoint | Use |
|----------|-----|
| `GET /health/liveness` | Orchestrator liveness |
| `GET /health/readiness` | Currently minimal 200; deepen for Dialogs/Secman per wiki P3 |
| `GET /api/health` | Public summary (see handler) |

Probe example:

```bash
curl -sf http://127.0.0.1:8080/health/liveness
```

---

## Local development

```bash
export OUROBOROS_ENV=development
export OUROBOROS_SERVER_HOST=127.0.0.1
python server.py
```

Development may enable extra settings keys ignored in production (`storage/settings/profile.py::PRODUCTION_IGNORED_SETTING_KEYS`).

---

## Convergence checklist (wiki P0–P3)

Use this when hardening a pilot environment:

- [ ] TLS termination at edge for any callback or admin path
- [ ] Ingress auth / private network for any non-loopback callback or admin path
- [ ] Secrets from Secman (target) — not committed env files
- [ ] Fixed skill/tool allowlist from release artifact (no marketplace install)
- [ ] Egress allowlist for LLM and Dialogs only
- [ ] Audit log forwarder (target) — runtime events are in metadata DB `audit_events`
- [ ] Readiness fails when Dialogs or LLM credentials are invalid (target)

Details: [Solution Architecture — Deployment](https://gitlab.k8s.genai.int/gen-ai/fusion_team/backend/ouroboros/-/wikis/Solution-Архитектура-As-Is#развертывание).
