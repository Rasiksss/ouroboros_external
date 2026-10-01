# Creating skills for Ouroboros (headless)

This guide describes the skill formats that the current headless runtime actually
discovers and executes. The sources of truth are
[`contracts/skill_manifest.py`](../ouroboros/contracts/skill_manifest.py),
[`skills/loader.py`](../ouroboros/skills/loader.py), and
[`contracts/plugin_api.py`](../ouroboros/contracts/plugin_api.py). See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for runtime wiring.

Ouroboros uses the word *skill* for both portable instruction packs and
Ouroboros-specific executable packages. Keep that distinction explicit:

- A portable instruction pack needs only `name` and `description` frontmatter
  plus concise Markdown instructions.
- Script, extension, and tool packages use additional fields understood by the
  Ouroboros manifest parser. Do not assume those fields are portable to another
  Codex or OpenClaw runtime.

## Choose the package type

| Type | Package contents | Runtime behavior | Use when |
|------|------------------|------------------|----------|
| `instruction` | `SKILL.md` and optional references/assets | Indexed for the agent; no code is executed | A playbook or domain guide is sufficient |
| `script` | `SKILL.md` plus declared files under `scripts/` | Runs through `skill_exec` in a sandboxed subprocess | Deterministic CLI or batch work |
| `extension` | `SKILL.md` plus a Python entry module, normally `plugin.py` | Imports in the server process and registers through `PluginAPI` | Bounded in-process tools, tasks, events, or chat injection |
| `tool` | `SKILL.md` plus `tool.py` | Imports native `ToolEntry` objects into the tool catalog | Direct native-tool compatibility is explicitly required |

Prefer `instruction` when instructions are enough and `script` when process
isolation is acceptable. Both `extension` and `tool` execute trusted Python in
the server process. `extension` is the preferred bounded API; `tool` is a more
direct integration and should be used deliberately.

## Package layout and naming

Use a short lowercase folder name containing letters, digits, and hyphens, and
make frontmatter `name` identical to it. Distribution discovery uses the manifest
name while runtime state uses a normalized identity; keeping them equal avoids mismatches. Selected names
are sanitized to at most 64 characters for state paths, so also avoid names that
normalize to the same value.

```text
my-skill/
├── SKILL.md              # preferred manifest and agent instructions
├── scripts/              # script payloads, when needed
├── references/           # detailed material loaded only when relevant
└── assets/               # templates or files used in produced output
```

Executable packages may additionally contain `plugin.py`, `tool.py`, or other
files explicitly referenced by the manifest. Keep `SKILL.md` concise; put long
schemas and variant-specific material in `references/`. The agent first loads
the selected manifest with `skill_read(skill="<name>")`, then loads only needed
reference documents with
`skill_read(skill="<name>", path="references/<file>")`. `assets/`, `scripts/`,
and executable entry modules are not documentation surfaces. Do not duplicate
the same instructions across files.

`skill.json` is accepted as an Ouroboros manifest, but `SKILL.md` is preferred
because it also carries instructions and is portable to instruction-pack
discovery. A discovered `SKILL.md` must be a regular file; manifest symlinks
are ineligible even when their target remains inside the package.

## Location and trust

Release-owned skills live only under `${OUROBOROS_REPO_DIR}/skills`. The runtime
does not copy them into `data/skills`, scan home-agent directories, or discover
legacy `native`, `external`, `clawhub`, or `ouroboroshub` buckets. Production must
mount or package the repository read-only as part of the trusted distribution.

## Instruction packs

An instruction pack should use the portable minimum:

```markdown
---
name: incident-triage
description: Triage service incidents from logs and health evidence. Use when the user asks to diagnose an outage, latency spike, or repeated runtime failure.
---

# Incident triage

1. Gather current health evidence.
2. Separate observed facts from hypotheses.
3. Report the smallest safe next action.
```

`type` defaults to `instruction`, so it may be omitted. Put all triggering
language in `description`: the discovery index exposes `name`, `description`,
and location before the full body is read.

Optional OpenClaw-compatible eligibility gates live under
`metadata.openclaw`:

```yaml
metadata:
  openclaw:
    os: [linux, darwin]
    requires:
      bins: [curl]
      env: [WEATHER_API_KEY]
      config: [feature.flags.weather]
    skillKey: weather
```

These gates affect discovery eligibility; they do not install missing software.
Instruction-pack discovery requires valid YAML frontmatter and a non-empty
`description`. It also applies OS, binary, environment, config, size, allowlist,
collision, and shadowing checks. It does **not** require `enabled.json` merely to
appear in the available-skills discovery index.

## Ouroboros manifest fields

`SKILL.md` frontmatter and `skill.json` use the same parser. Common fields are:

| Field | Meaning |
|-------|---------|
| `name` | Human-facing name; always provide it even though the loader can fall back to the folder name |
| `description` | Capability and triggering contexts; required for discovery eligibility |
| `version` | Package version shipped with the distribution |
| `type` | `instruction`, `script`, `extension`, or `tool` |
| `when_to_use` | Additional Ouroboros trigger metadata for enabled-skill summaries |
| `runtime` | Subprocess runtime for `script`; it does not select an extension runtime |
| `timeout_sec` | Script timeout request; `skill_exec` caps it at 300 seconds |
| `scripts` | Executable allowlist for `script` packages |
| `entry` | Python entry file for `extension`; optional for `tool`, which defaults to `tool.py` |
| `permissions` | Declared extension capabilities and `PluginAPI` method gates |
| `env_from_settings` | Settings names that may be projected into a script env or read through `PluginAPI` |
| `subscribe_events` | Event topics an extension may subscribe to |
| `companion_processes` | Reviewed subprocess descriptors used by an extension |
| `dependencies`, `install`, `install_specs` | Optional dependency metadata normalized from extra frontmatter |

Unknown fields are retained as `raw_extra` for compatibility. Do not treat that
as proof that a field has runtime behavior. In particular, `ui_tab`, widget,
route, and WebSocket surfaces are not supported by the headless runtime.

## Script packages

Minimal `SKILL.md`:

```yaml
---
name: report-builder
description: Build a deterministic report from supplied input files. Use when the user asks to generate the supported report format.
version: 1.0.0
type: script
runtime: python3
timeout_sec: 60
scripts:
  - name: scripts/build_report.py
env_from_settings: []
---

# Report builder

Call `skill_exec` with `scripts/build_report.py` and explicit scalar arguments.
```

Only entries declared under `scripts` can execute. A bare entry such as
`build_report.py` resolves under `scripts/`; an explicit
`scripts/build_report.py` is also accepted. Runtime values are restricted to
`python`, `python3`, `bash`, `node`, `deno`, `ruby`, and `go`, and the matching
host binary must exist.

`skill_exec` supplies a minimal environment, the skill name, and a private state
directory. It confines cwd, rejects path traversal, applies a timeout, caps
stdout/stderr, and does not install dependencies on demand.

## Extension packages

Extensions are Python modules loaded in the server process. `runtime` is ignored
for extension dispatch; `entry` must resolve to a Python file inside the package.

Minimal `SKILL.md`:

```yaml
---
name: lookup-service
description: Look up records from the configured service. Use when a user asks for a supported record lookup.
version: 1.0.0
type: extension
entry: plugin.py
permissions: [tool]
env_from_settings: []
---

# Lookup service

Use the registered lookup tool.
```

Minimal `plugin.py`:

```python
def lookup(_ctx, query=""):
    return f"lookup result for {query}"


def cleanup():
    pass


def register(api):
    api.register_tool(
        "lookup",
        lookup,
        description="Look up one record",
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        log_redacted_args=["query"],  # omit sensitive argument values from tool-call logs
    )
    api.on_unload(cleanup)
```

`register_tool` also accepts an optional `log_redacted_args` keyword: a list of
argument names (validated against the schema properties) whose values are
omitted from tool-call logs. Declare it for payload arguments that must never
be logged.

Further optional `register_tool` keywords:

- `result_limit` — per-tool char cap the core enforces on the tool result
  (`None` = the runtime default). Must be a positive int no larger than
  `RESULT_LIMIT_HARD_MAX` (120 000, `ouroboros/tools/catalog.py`); larger
  values are rejected at registration.
- `retrieval_aliases` (1.8) — a list/tuple of non-empty search terms merged
  into working-memory retrieval indexing for this tool's invocations (e.g.
  localized tool names).
- `parallel_safe` (1.8) — a bool declaring the tool safe to run inside a
  parallel read-only tool-call batch. Leave it `False` (the default) for
  anything mutative or delivery-like. `True` takes effect only together with
  `safety_policy="skip"` — a parallel batch must not stack blocking
  safety-supervisor calls, so without `"skip"` the tool registers as not
  parallel-safe (with a warning).
- `safety_policy` (1.9) — `"check"` or `"skip"`. `"check"` (the default when
  unstated) routes every call through the LLM safety supervisor before it runs;
  `"skip"` is for read-only tools with no external side effect. The policy is
  applied by the core dispatcher on the canonical namespaced name — a tool no
  longer needs its own in-plugin gate, and being distribution-owned no longer
  bypasses safety.
- `capability_class` (1.9) — one of `"read_only"`, `"mutative"`,
  `"external_network"`; classifies what the tool touches. Unstated falls back to
  the core default. Use `"external_network"` for anything that leaves the host
  (delivery, outbound calls).

The frozen ABI is `ouroboros/contracts/plugin_api.py` v1.9. Its current methods
cover:

- tool, supervised-task, and companion-process registration;
- event subscription;
- agent chat injection, direct outbound messaging, and internal chat-id allocation;
- unload callbacks and structured logging;
- allowlisted settings, namespaced durable state, private job directories, and runtime information.

Declare the matching permission before calling a gated method:
`tool`, `supervised_task`, `companion_process`, `subscribe_event`,
`inject_chat`, `send_message`, `read_settings`, or `fs` for durable state. Event subscriptions must also be declared in
`subscribe_events`.

Do not call `inject_chat` at module import time or as an unconditional startup
side effect. Capture `api` inside a registered handler or supervised task and
inject only as part of the intended workflow. Use `send_message` when text is
already final and no agent reasoning is required.

`net`, `fs`, and `subprocess` are accepted capability declarations, but they are
not a Python sandbox. Extension code runs in-process and must be trusted and
reviewed accordingly.

## Tool packages

`type: tool` imports native `ToolEntry` declarations rather than using
`PluginAPI`. Use it only when that direct contract is needed.

Minimal `SKILL.md`:

```yaml
---
name: text-tools
description: Provide deterministic text transformations. Use when the user asks for one of the bundled transformations.
version: 1.0.0
type: tool
entry: tool.py
---

# Text tools

Use the namespaced transformation tools.
```

Minimal `tool.py`:

```python
from new_core.tools import ToolEntry


def echo(_ctx, text=""):
    return text


def get_tools():
    return [
        ToolEntry(
            name="echo",
            schema={
                "name": "echo",
                "description": "Return the supplied text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            handler=echo,
        )
    ]
```

`ToolEntry` accepts the same runtime metadata as `register_tool`, with the same
allowed values and the same `parallel_safe` rule: `safety_policy`,
`capability_class`, `result_limit`, `parallel_safe`, `retrieval_aliases`, and
`log_redacted_args`. All are optional; anything unstated falls back to the core
defaults (`safety_policy="check"`, `capability_class="read_only"`). The values
survive namespacing and apply under the `<skill>.<tool>` alias. A declaration
the extension loader would reject (an unknown policy or class, redacted args
missing from the schema) makes the skill load no tools.

The registry exposes a namespaced alias derived from the skill and tool names;
it never replaces a built-in tool. A tool package must be selected/eligible,
enabled, free of `load_error`, and have a grant record matching its current
content hash before import. Its entry is syntax-checked by `validate_skill`.

## Dependencies

Executable manifests may declare isolated dependency metadata. A string list is
treated as Python packages:

```yaml
dependencies:
  - httpx==0.28.1
```

Object specs may use `install` or `install_specs`:

```yaml
install:
  - kind: pip
    package: httpx==0.28.1
```

`pip`, `pipx`, `uv`, `node`, and `npm` specs with safe package names can be
installed into a per-skill isolated prefix. `brew`, `apt`, `go`, downloads,
Cargo, and unsafe/unknown specs are manual setup; Ouroboros does not mutate
global host state for them. Script execution and extension loading stay blocked
while declared automatic dependencies are absent or stale. Provision them
through the owner/lifecycle installation path before enabling execution.

## Validation and inspection

Use the three read-only/validation surfaces for different questions:

| Tool | Answers |
|------|---------|
| `check_skills` | Which distribution skills were found and why a skill is ineligible |
| `list_skills` | Full manifest/runtime status, or compact cursor-paginated discovery with `view="discovery"` |
| `skill_read` | The selected eligible package's `SKILL.md` or a needed UTF-8 document below `references/` |
| `skill_preflight` | Whether discovered payload files parse and pass offline syntax/path checks |

Examples:

```text
check_skills()
list_skills()
skill_read(skill="incident-triage")
skill_read(skill="incident-triage", path="references/provider-notes.md")
skill_preflight(skill="report-builder")
skill_preflight(skill="lookup-service", paths=["plugin.py"])
```

Follow every continuation footer until the document is complete. Normal
line-bounded pages return the next `start_line`; a page cut inside a long line
returns `start_char`, whose absolute character offset preserves the line while
every tool result stays inside the runtime result limit.

`skill_preflight` does not call an LLM. It parses the manifest, checks referenced
entries/scripts, and runs bounded syntax validators such as Python compile,
`node --check`, and `bash -n`.

Important limitation: the current preflight implementation does **not** call
`SkillManifest.validate()`. A parseable manifest can therefore preflight
successfully while still carrying semantic warnings such as an unknown runtime,
an unknown permission, a missing extension entry declaration, or an empty
script allowlist. Cover the package type with unit tests and verify the final
runtime status with `check_skills`/`list_skills`; preflight is not an approval or
trust decision.

## Trust, readiness, and dispatch

The executable types have different final gates:

| Type | Final gates | Dispatch |
|------|-------------|----------|
| `instruction` | Selected and discovery-eligible | Agent reads `SKILL.md` through `skill_read` and follows it |
| `script` | Parseable payload, dependencies ready, runtime and declared script available | `skill_exec` subprocess |
| `extension` | Parseable payload, dependencies ready, entry imports and `register(api)` succeeds | Live extension tools/tasks/events |
| `tool` | Selected/eligible, entry imports and `get_tools()` succeeds | Namespaced catalog entries |

Distribution skills are always enabled. Manual `enabled.json` and `grants.json`
files are ignored; declared manifest permissions and `env_from_settings` entries
are auto-authorized because the repository itself is the trust boundary. PluginAPI
still denies any capability or setting not declared by that manifest. There is no
tri-model review gate. An extension is ready only after it is actually live-loaded;
any distribution extension load error blocks startup.

## Repair mode

Runtime skill repair is unsupported: the trusted repository is read-only in
production. Edit and test the release repository, then build a new distribution.

## Headless limitations

The current headless runtime exposes no extension HTTP routes, UI tabs, widget
rendering, WebSocket handlers, or `host_service` token injection. Do not use
`route`, `widget`, `ui_tab`, `ws_handler`, or `iframe_raw` as authoring targets,
even though the broad manifest parser still recognizes some legacy tokens.

Marketplace HTTP install and `submit_skill_to_hub` are absent. Legacy marketplace
payloads are ignored.

## Release and production delivery

Repository skills under `skills/` are the release payloads and execute directly
from the read-only distribution. Extension imports use an ephemeral `/tmp` copy;
private runtime state remains under `data/state/skills/<name>/`.

Fixed release skill/tool allowlist is not implemented by this runtime; it remains
an explicit deployment TODO. Do not describe bundled hashes or CI promotion as
an enforcement gate unless the deployment pipeline really supplies and verifies
one. Production operators must provide trusted build, review, signing/allowlist,
and promotion controls outside the current runtime where required by policy.

The internal [Skills wiki](https://gitlab.k8s.genai.int/gen-ai/fusion_team/backend/ouroboros/-/wikis/Solution-Архитектура-As-Is/Skills)
describes the production target. When it differs from executable code and
tests, document the gap instead of presenting the target as implemented.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Missing from the prompt index | Run `check_skills`; inspect frontmatter, gates, allowlist, collision, and shadowing reasons |
| `skill_disabled` | Enable through the owner/admin provisioning surface |
| `missing_grants` | Approve the requested keys/permissions for the current content hash |
| Dependency readiness error | Re-run the owner/lifecycle isolated-dependency install after manifest changes |
| `load_error` | Fix manifest parsing, unreadable payload files, or extension import/registration errors; inspect logs |
| `SKILL_EXEC_BLOCKED` | Check enabled state, grants, dependencies, runtime, and declared script path |
| Extension tool absent | Confirm startup/reconcile loaded the extension and `register(api)` completed |
| Tool package absent | Confirm eligibility, enabled state, hash-bound grant record, syntax, and `get_tools()` |

This guide describes the headless runtime — the only supported runtime of
this repository.
