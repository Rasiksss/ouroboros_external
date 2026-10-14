# tool-providers Specification

## Purpose

Tool sources behind one interface. Every kind of source — repo-defined tools, `type: tool` skills,
`type: extension` skills, MCP servers, and remote A2A agents — is reached through the same provider
interface, and every tool it yields is described by one immutable descriptor that carries its
model-facing schema and its policy together. Providers are ordered, and that order alone decides
name collisions and alias resolution; a provider resolves its own alias forms and answers with
canonical names. Each provider receives its configuration through its constructor rather than
reading process globals, and holds no task state: `ToolContext` is an argument to `execute`, never a
field. The cross-cutting gates — excluded surface and the LLM safety supervisor — run once above
the providers, so a provider's `execute` is only its source-specific
invocation. A running tool reaches the catalog through its context, with reads and a request to
widen the surface, and no way to invoke another tool. Where a tool body runs is a separate
capability (`tool-runtimes`).
## Requirements
### Requirement: Every tool source is reached through one provider interface

Tools SHALL reach the runtime only through implementations of a single provider interface, one per
source kind: repo-defined tools, `type: tool` skills, `type: extension` skills, MCP servers, and
remote A2A agents. The interface SHALL expose catalog reads (`descriptors`, `get`,
`refuse_dispatch`), source-specific
execution (`execute`), and lifecycle (`refresh`, `close`), and nothing else. Every member but
`close` SHALL be required of an implementation rather than defaulted, so that each source answers
for itself. No consumer SHALL read a provider's internal storage directly.

#### Scenario: A consumer enumerates tools without knowing the source

- **WHEN** a consumer asks the ordered provider list for descriptors
- **THEN** tools from all five sources are returned in one sequence
- **AND** the consumer performs no source-specific branching to obtain them

#### Scenario: A new provider cannot inherit an answer it was never asked for

- **WHEN** a provider is written without implementing one of the required members
- **THEN** it cannot be instantiated

#### Scenario: Extension tools are no longer read through the module global

- **WHEN** prompt-surface assembly or the skills summary needs live extension tools
- **THEN** they are obtained through the extension provider's injected runtime interface
- **AND** no module outside `ouroboros/skills/extensions/loader.py` imports `_tools` or `_lock`

### Requirement: One descriptor shape carries every tool, including its policy

Every tool SHALL be described by a single immutable descriptor. Its model-facing part SHALL be the
existing tool schema type — name, description, and parameters — held as one value rather than
re-declared as separate fields, so no conversion is needed to place a tool in a model request. The
descriptor SHALL additionally carry timeout, origin, prompt surface, capability class, safety
policy, and whether the tool skips the LLM safety supervisor. The descriptor's canonical name SHALL
be the name in its schema. Policy fields SHALL be populated by the provider rather than defaulted at
the point of use.

#### Scenario: A distribution-owned extension tool gets no safety exemption

- **WHEN** the extension provider builds a descriptor for a tool registered by a distribution-owned
  skill
- **THEN** the descriptor does not mark the tool as skipping the LLM safety supervisor
- **AND** dispatch applies the tool's declared safety policy; a tool that declared none is checked

#### Scenario: A declared safety policy is applied by the canonical name

- **WHEN** a skill or extension tool registered with an explicit safety policy is dispatched
- **THEN** dispatch passes the descriptor's declared policy to the safety supervisor
- **AND** the policy applies under the canonical namespaced name, not the pre-namespacing one

#### Scenario: Origin identifies the owning source

- **WHEN** a descriptor is produced by any provider
- **THEN** its origin identifies the source kind and, for skill and MCP tools, the owning skill or
  server
- **AND** consumers needing per-skill grouping or an "is extension tool" check derive it from origin

#### Scenario: A descriptor goes into a model request without conversion

- **WHEN** prompt-surface assembly places a tool into a model request
- **THEN** it uses the schema held by the descriptor directly
- **AND** no per-source schema-building step runs

### Requirement: Provider configuration is injected, not discovered

A provider SHALL receive its configuration — source loader and root, MCP server list, event loop —
through its constructor. `refresh()` SHALL take no arguments and SHALL be safe to call more than once.
A provider SHALL NOT read process-global configuration to learn where its tools live. The
application-owned distribution loader receives validated settings and feature-pack policy before it
is injected; `DistributionSkillProvider` names neither application setting nor filesystem policy.

#### Scenario: Two distribution providers coexist with different roots

- **WHEN** two `DistributionSkillProvider` instances are constructed with different roots
- **THEN** each asks its injected loader for the records under its own configured root
- **AND** neither discovers an additional root on its own

Skill-backed tool providers SHALL receive an already-composed `SkillSurface`. They SHALL NOT walk
skill roots or refresh the skill catalog themselves; the composition root owns both dependencies and
publishes them before admitting tasks. The task path SHALL refresh neither catalog.

#### Scenario: Skill tools use the composed package snapshot

- **WHEN** `SkillToolProvider` refreshes its tool descriptors
- **THEN** it reads package records from its injected deployment skill surface
- **AND** it performs no package discovery or skill-catalog refresh

#### Scenario: MCP asynchrony stays inside its provider

- **WHEN** the MCP provider is refreshed
- **THEN** it performs its asynchronous discovery internally
- **AND** the caller observes a synchronous `refresh()` like every other provider

### Requirement: Skill packages have source-qualified identity and one catalog

Every available skill package SHALL carry a `SkillId` made of a non-empty `source_id` and
source-local stable identifier. Its canonical string SHALL be `source_id:local_id`; declared version,
content hash and display metadata SHALL NOT participate in identity. A process-wide `SkillCatalog`
SHALL combine ordered `ISkillProvider` snapshots and reject duplicate provider source ids.

Executable surfaces and private runtime resources SHALL use the complete `SkillId`, not the display
alias. This includes tool namespaces, extension registries, modules, and private state directories.
A derived runtime key SHALL keep legacy simple distribution keys where they
cannot overlap the reserved qualified-key space; every other id SHALL receive a path-safe digest key,
and path handling SHALL preserve that digest rather than truncating it.

Full ids SHALL resolve only through their named provider. Unqualified names, display names and legacy
skill keys SHALL NOT resolve a package. A malformed string containing the source separator SHALL fail
closed. Physical records with one stable id SHALL resolve through the selected record without altering
provider-local `selected` state.

Model-facing `type: tool` names SHALL use the shared provider-safe name grammar and SHALL retain
source-qualified identity after lossy character normalization and length bounding.

#### Scenario: Two sources expose the same display name

- **WHEN** two providers contain skills with the same display name
- **THEN** both packages remain reachable through their full ids
- **AND** the display name resolves to neither package
- **AND** neither package's provider-local selection state is rewritten

#### Scenario: A provider refresh fails

- **WHEN** rebuilding a provider snapshot raises
- **THEN** the previous complete tuple and lookup index remain published
- **AND** no partial snapshot becomes visible

### Requirement: Skill availability is a projection over the catalog

`SkillSurface` SHALL apply provider-global validation, the existing default and agent-specific
deployment allowlists, and, for a multi-user task surface, the owner-and-agent enabled-skill
allowlist. It SHALL make a separate record when adding the legacy deployment allowlist block reason
and SHALL NOT mutate catalog records. Owner selection SHALL instead omit every non-enabled record
from task `skills()` and make task `get()` return no record, so an owner cannot inspect a package
outside the allowlist through the full status view or direct canonical lookup.

A run surface SHALL be bound to the run's complete `OwnerScope`. Before prompt assembly, task startup
SHALL resolve `OUROBOROS_USER_ENABLED_SKILL_IDS` once for that scope, intersect it with the ids in the
published snapshot, and freeze the resulting set for the invocation. A task-facing argument SHALL
NOT select another owner or agent's projection. A failure to resolve the allowlist SHALL produce an
empty task skill set without failing the task or removing tools of other origins.

Prompt and paginated discovery SHALL publish the canonical skill id used for `skill_read`; a
source-qualified enabled record therefore remains addressable independently of its display name. An
allowlist entry SHALL use the canonical `source_id:local_id`. A malformed, display-name or legacy
skill-key entry SHALL invalidate the allowlist and fail closed for skills. A canonical entry SHALL
enable only the skill from that source, even when another provider publishes the same display name.
An unknown canonical id SHALL expose nothing.

The owner allowlist SHALL only narrow deployment policy. A record removed by discovery policy,
ineligible under provider validation, blocked by the existing agent allowlist, or withheld by the
multi-user executable cutoff SHALL remain unavailable even when the owner allowlist names it.

The surface SHALL receive typed immutable deployment availability and a frozen effective task
selection rather than raw settings. Production assembly SHALL pass every skill surface and skill
permission checker as a required named dependency; neither may be auto-created by an agent or
adapter.

Task-facing skill operations and prompt assembly SHALL use the run's skill surface. Extension
lifecycle and skill-backed tool providers SHALL use the unbound deployment surface. A task-facing
skill tool without a surface SHALL fail closed with `SKILLS_UNAVAILABLE` rather than rediscovering
packages. The deployment surface SHALL apply no owner's selection and SHALL expose the union permitted
by the default and every configured agent allowlist, so code usable by no deployment agent is not
loaded while a named agent's package remains usable.

#### Scenario: An agent allowlist blocks a skill

- **WHEN** a run projects a catalog record excluded by its existing agent allowlist
- **THEN** the projected record carries `agent allowlist blocks this skill`
- **AND** the catalog record remains unchanged for another run

#### Scenario: An owner allowlist selects one source-qualified skill

- **WHEN** `distribution:calendar` and `openclaw:calendar` share a display name
- **AND** the deployment allowlist permits both
- **AND** the owner allowlist names only `distribution:calendar`
- **THEN** only `distribution:calendar` is visible on that task surface

#### Scenario: Two owners sharing an agent id receive different surfaces

- **WHEN** two runs use the same agent id under different owners with different stored allowlists
- **THEN** each run surface exposes only its owner's enabled catalog records
- **AND** the catalog snapshot and the other run surface remain unchanged

#### Scenario: Absence uses the instance allowlist

- **WHEN** a multi-user task starts for an owner and agent with no enabled-skill row
- **THEN** its prompt, full and compact listings, checks, and direct lookups expose only skills named
  by the instance allowlist and allowed by catalog, deployment allowlist, and executable cutoff
- **AND** the unbound deployment surface remains unchanged

#### Scenario: Empty effective allowlist hides every skill

- **WHEN** the effective enabled-skill list is empty
- **THEN** the task skill surface is empty
- **AND** direct lookup opens no manifest or package document

#### Scenario: A non-enabled canonical id is hidden before file access

- **WHEN** a task calls `skill_read` with the exact canonical id of a skill outside its allowlist
- **THEN** lookup answers as if the package does not exist
- **AND** no manifest or package document is opened for that request

#### Scenario: Allowlist failure does not fail unrelated work

- **WHEN** owner-setting resolution raises or returns a malformed allowlist while a task surface is being built
- **THEN** the task receives an empty skill surface
- **AND** the task continues with every otherwise available non-skill tool

#### Scenario: A task context has no skill surface

- **WHEN** list, read, or execution needs a skill package
- **THEN** the operation reports that the skill surface is unavailable
- **AND** it does not fall back to direct filesystem discovery

### Requirement: Name resolution and collisions are decided by provider order

The provider list SHALL be ordered, and a name present in more than one provider SHALL resolve to
the first provider in that order. A provider SHALL resolve its own alias forms internally, so a
lookup by alias returns a descriptor whose `name` is canonical.

#### Scenario: A skill tool cannot shadow a built-in

- **WHEN** a `type: tool` skill exposes a name already registered by a repo-defined tool
- **THEN** the repo-defined tool resolves
- **AND** the collision is logged once during refresh

#### Scenario: An extension alias resolves to the canonical name

- **WHEN** a caller looks up an extension tool by its `<skill>.<tool>` alias
- **THEN** the extension provider returns the descriptor for that tool
- **AND** the descriptor's name is the provider-safe canonical form

### Requirement: A name withheld from the surface is explained, not reported as unknown

Resolution SHALL answer whether a tool may be *offered*, and a provider MAY withhold a name it owns —
a not-live extension's tool must reach no surface read, or the listing and the enable path
disagree and the tool lands on the prompt surface refusing every call for the rest of the task.
Every provider SHALL answer, for a name it does not resolve, whether it owns that name and what to
say about it — `None` where a source can withhold nothing. Dispatch SHALL ask for that refusal
before concluding the name is unknown, and SHALL ask before the LLM safety supervisor runs, so that
a call which cannot run costs no model call.

#### Scenario: Dispatch to a not-live extension names the reason

- **WHEN** a task calls an extension tool whose extension is no longer live
- **THEN** the caller is told the extension may not dispatch, not that the tool is unknown
- **AND** the extension is unloaded, so the next task does not pay for it again
- **AND** the safety supervisor is not consulted

#### Scenario: A name no provider owns is still unknown

- **WHEN** a task calls a name no provider resolves and none claims
- **THEN** the caller is told the tool is unknown, with the repo and skill tools listed

### Requirement: Cross-cutting gates run once, outside the providers

The excluded-surface check, LLM safety supervisor, and result handling SHALL be applied in one place before
delegating to a provider. A provider's `execute` SHALL implement only its source-specific
invocation. `ToolContext` SHALL be passed into `execute` as an argument and SHALL NOT be stored on
the provider.

#### Scenario: The active-tool gate covers remote agents

- **WHEN** a task with a restricted active tool set calls a remote-agent tool that is not in that set
- **THEN** the call is gated exactly as an MCP tool would be

#### Scenario: Concurrent tasks do not share tool context

- **WHEN** a tool is executed for a task
- **THEN** the context used is the one passed in for that task
- **AND** no provider retains it after the call returns

### Requirement: Tools reach the catalog through the tool context

`ToolContext` SHALL expose a tool-surface interface providing catalog reads and the set of currently
active tool names, plus a request to add a tool to the active surface. The interface SHALL NOT
provide a way to invoke another tool. The active-name snapshot SHALL include requests already made
during the current round.

The reads SHALL be narrowed to what the run may use: a tool a scope-aware provider excludes SHALL
be absent from the catalog read and SHALL NOT resolve by name,
so a run is never offered a tool its own dispatch would refuse. The narrowing SHALL be applied
where the surface is built for the run, over the one process-wide catalogue, rather than by
building a second catalogue or a second provider list per run.

#### Scenario: The listing tool reads the catalog from its context

- **WHEN** `list_available_tools` runs
- **THEN** it obtains candidates and the active set from its tool context
- **AND** it requires no injected handler and no module-global registry reference

#### Scenario: Two enable calls in one round see each other

- **WHEN** `enable_tools` is called twice in the same round for the same tool name
- **THEN** the first call reports the tool as enabled
- **AND** the second call reports it as already active

#### Scenario: An unknown tool name is reported synchronously

- **WHEN** `enable_tools` is called with a name absent from every provider
- **THEN** the call returns a not-found result for that name in the same invocation
- **AND** no enable request is recorded for it

#### Scenario: A tool the run may not use is not on its surface

- **WHEN** the surface is built for a run that may not use one of the catalogue's tool sources
- **THEN** that source's tools are absent from the catalog read and do not resolve by name
- **AND** the process-wide catalogue is unchanged and serves other runs in full

### Requirement: The catalogue is not refreshed on the task path where skills cannot change

The runtime SHALL publish the skill catalogue from read-only provider inputs before admitting tasks.
Runtime tasks SHALL NOT repair, enable, grant, provision dependencies for, or finalize skill packages.
Provider-specific acquisition and sandbox execution remain separate future capabilities.

Resolving an owner-and-agent allowlist SHALL project only the already-published snapshot. It SHALL
NOT refresh a provider or catalog, walk a skill root, parse a manifest, recompute payload hashes,
import a package, or create a per-owner catalog. Manifest and payload document reads requested through
`skill_read` remain on-demand reads of a record already admitted by the task surface.

#### Scenario: A task uses a published skill

- **WHEN** a task lists, reads, or executes an available skill
- **THEN** it uses the surface derived from the startup-published snapshot
- **AND** it writes neither the package nor lifecycle authorization files

#### Scenario: Owner selection performs no discovery

- **WHEN** two owners start tasks with different skill allowlists
- **THEN** both task surfaces reference the same startup-published catalog generation
- **AND** neither task refreshes a provider, scans the filesystem, or constructs another catalog

#### Scenario: A task-facing skill operation has no surface

- **WHEN** a task-facing skill operation is invoked without its composed skill surface
- **THEN** it returns the stable `SKILLS_UNAVAILABLE` refusal to the model
- **AND** the tool executor records that result as an error
- **AND** no provider refresh or filesystem discovery is attempted

### Requirement: The skill core receives application effects through DI

`new_core.skills` SHALL own only skill models, manifest shape, interfaces, catalog, surface,
permission decisions and providers over injected sources. It SHALL import neither `ouroboros` nor
`transport`. Filesystem discovery, application settings, extensions and companion processes SHALL
remain application adapters. Composition roots SHALL inject the loader into the distribution
provider and pass the resulting snapshot and availability through the runtime to each agent.

#### Scenario: Importing the skill core does not load the application

- **WHEN** every module under `new_core.skills` is imported in a fresh interpreter
- **THEN** no `ouroboros` or `transport` module is loaded

#### Scenario: A task receives the composed skill generation

- **WHEN** a composition root builds an agent runtime
- **THEN** it passes the startup-published snapshot and availability explicitly
- **AND** no module-global skill catalog is read on the task path

