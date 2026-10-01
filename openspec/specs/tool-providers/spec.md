# tool-providers Specification

## Purpose

Tool sources behind one interface. Every kind of source — repo-defined tools, `type: tool` skills,
`type: extension` skills, MCP servers, and remote A2A agents — is reached through the same provider
interface, and every tool it yields is described by one immutable descriptor that carries its
model-facing schema and its policy together. Providers are ordered, and that order alone decides
name collisions and alias resolution; a provider resolves its own alias forms and answers with
canonical names. Each provider receives its configuration through its constructor rather than
reading process globals, and holds no task state: `ToolContext` is an argument to `execute`, never a
field. The cross-cutting gates — excluded surface, heal mode, payload checks, the LLM safety
supervisor — run once above the providers, so a provider's `execute` is only its source-specific
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
- **THEN** they are obtained through the extension provider
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

A provider SHALL receive its configuration — drive root, workspace root, settings reader, MCP server
list, event loop — through its constructor. `refresh()` SHALL take no arguments and SHALL be safe to
call more than once. A provider SHALL NOT read process-global configuration to learn where its tools
live.

#### Scenario: Two providers of the same kind coexist with different roots

- **WHEN** two skill-tool providers are constructed with different drive roots
- **THEN** each enumerates only the skills under its own root

#### Scenario: MCP asynchrony stays inside its provider

- **WHEN** the MCP provider is refreshed
- **THEN** it performs its asynchronous discovery internally
- **AND** the caller observes a synchronous `refresh()` like every other provider

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

Resolution answers whether a tool may be *offered*, and a provider MAY withhold a name it owns —
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

#### Scenario: Repair restrictions still win

- **WHEN** a task under a skill-repair constraint calls a withheld extension tool
- **THEN** the repair refusal is returned, ahead of the provider's own

### Requirement: Cross-cutting gates run once, outside the providers

The excluded-surface check, skill-repair (heal mode) restrictions, the skill-payload short-form
check, the LLM safety supervisor, and result handling SHALL be applied in one place before
delegating to a provider. A provider's `execute` SHALL implement only its source-specific
invocation. `ToolContext` SHALL be passed into `execute` as an argument and SHALL NOT be stored on
the provider.

A gate that selects tools by origin SHALL name every remote source it means to cover. Extension,
MCP, and A2A tools all reach outside the process, so a gate written for the first two SHALL cover
A2A as well rather than letting it through by omission.

#### Scenario: Heal mode blocks extension, MCP, and A2A tools uniformly

- **WHEN** a task running under a skill-repair constraint calls an extension tool, an MCP tool, or a
  remote-agent tool
- **THEN** the call is refused before any provider is reached

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
