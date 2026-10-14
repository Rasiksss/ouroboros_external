# multiuser-mode Specification

## Purpose
How the runtime splits into independent processes, each serving one tier of the multi-user deployment.
## Requirements
### Requirement: One flag declares the mode

Whether a deployment serves several owners SHALL be declared by exactly one setting, defaulting to
off. Behaviours that only hold together — identity resolved at the boundary, per-owner storage, a
fixed tool catalogue, the exclusions below — SHALL be governed by that one declaration and SHALL NOT
each have a switch of their own.

Independent switches let a deployment combine the behaviours wrongly with nothing to say so:
resolving real owners without per-owner storage is a leak, and running several tasks without the
one-run-per-owner rule is a race. A single declaration makes those combinations unreachable rather
than merely discouraged.

#### Scenario: One setting governs the mode

- **WHEN** the settings surface is inspected
- **THEN** exactly one boolean declares the mode
- **AND** no second setting expresses part of the same fact

#### Scenario: The default is unchanged behaviour

- **WHEN** the flag is absent or false
- **THEN** the instance behaves exactly as it did before the mode existed

### Requirement: The mode shapes composition, not every branch

The declaration SHALL be read once, at startup, and SHALL determine what the composition root builds.
Call paths SHALL NOT consult it to decide how to behave.

A flag read deep in call paths produces two code paths that drift apart, and the disabled one stops
being exercised by the enabled one's tests. A flag that selects which object is constructed leaves
one code path with a different collaborator behind it.

A deployment MAY start more than one kind of process from this repository, each with a composition
root of its own — a full runtime, and a transport tier that serves the same owners while running no
supervisor and no agent runtime. Every such root SHALL read the declaration the same way and SHALL
build the whole set of collaborators the mode implies for what that root composes; a root that
composes an owner-facing surface without the mode's collaborators behind it SHALL refuse to start
rather than serve. Otherwise the mode holds only in the process that happens to be the one everybody
tests, and a second entrypoint silently serves every caller as one owner.

#### Scenario: The flag is not read on the task path

- **WHEN** the code paths that handle a message, run a task, or execute a tool are inspected
- **THEN** none reads the mode setting
- **AND** what differs between modes is which collaborator they were given

#### Scenario: A second entrypoint composes the mode the same way

- **WHEN** the transport entrypoint starts with the mode declared
- **THEN** it reads the same declaration, from the same place, as the full runtime
- **AND** it composes the mode's collaborators for every surface it mounts

#### Scenario: A root that cannot serve the mode refuses

- **WHEN** an entrypoint would mount an owner-facing surface with the mode declared and one of the
  mode's collaborators not composed
- **THEN** startup fails naming what is missing, and no surface is served

### Requirement: A restart is refused while the instance serves several owners

With the mode declared, `/restart` SHALL be answered with a refusal that names the reason: the command
stops the process together with runs belonging to several owners. Nothing SHALL be cancelled or stopped
on its account. The refusal SHALL be the restart hook the
composition root hands the supervisor, so the command path reads no mode.

#### Scenario: An owner's restart is refused

- **WHEN** the mode is declared and an owner issues `/restart`
- **THEN** the command is answered with a refusal that says the instance serves several owners
- **AND** no run is cancelled, and the process does not stop

### Requirement: Shared external integrations are declared, not discovered

An integration the instance holds on everyone's behalf — one set of MCP servers and one set of outbound
A2A agents for the whole deployment, each reached over a connection every owner's calls share — SHALL
be documented as shared where the mode is documented. The documentation SHALL state, for MCP and A2A
alike and for each authorization kind, whose identity a call leaving the instance carries:

- a `user_headers` call carries the recorded browser credentials of the user whose run made it, and a
  `user_token` call that carries a personal token carries the token that owner stored for that server
  or remote agent under the run's agent; such a call SHALL be documented as attributable to that
  person;
- a `none` or `mtls` call, and a `user_token` call that carries the entry's static token, carry the
  deployment's identity or none; such a call SHALL be documented as not attributable to a person,
  because the external service cannot tell whose call it was.

An authorization kind SHALL NOT be documented as attributable on the strength of the entry's
configuration alone: a `user_token` entry sends its static token wherever no personal token is looked
up or stored, and that call leaves under the deployment's identity.

An accepted limitation that is written down is a decision; the same limitation found later by reading
the code is a defect report.

#### Scenario: The shared integration is documented as shared

- **WHEN** the mode's documentation is read
- **THEN** it states which external integrations are shared by every owner, naming the MCP servers and
  the outbound A2A agents
- **AND** it states, for each authorization kind, whether a call leaving the instance is attributable
  to a person

#### Scenario: A call carrying a person's credentials is documented as attributable

- **WHEN** the mode's documentation describes `user_headers`, and `user_token` with a personal token
- **THEN** it states that such a call carries that user's or owner's own credentials and is
  attributable to that person, for MCP and for A2A

#### Scenario: A call under the deployment's identity is documented as not attributable

- **WHEN** the mode's documentation describes `none`, `mtls`, and a `user_token` call that carries the
  static token
- **THEN** it states that such a call leaves under the deployment's identity, or none, and is not
  attributable to a person, for MCP and for A2A

### Requirement: Every ingress that mints the configured owner carries its own refusal

The mode's refusal set SHALL contain a refusal for **every** ingress that stamps the instance's
configured owner onto a message that came from a person, not for whichever one was noticed first. Each
such refusal SHALL name the ingress, why it cannot serve several owners, and the setting that disables
it, and SHALL be collected with the other refusals rather than stopping the check.

A single named refusal reads as a complete rule and is not one. The mode's promise is that a message
is attributed to the person who sent it; an ingress that flattens every sender to one owner breaks
that promise identically no matter which ingress it is, so the exclusion belongs to the property and
not to a list that happens to have one entry. The failure is silent by construction: the instance
starts, reports the mode accepted, stores the messages under the wrong owner, and every downstream
guarantee that reads the owner axis — history, queue admission, storage keys — is then correct about
an identity nobody presented.

Dialogs SHALL be a member of that set. It builds the scope for every arriving chat from the
instance's configuration, in one place and with no input from the message, so no configuration of it
can attribute a sender.

#### Scenario: A configured Dialogs ingress is refused

- **WHEN** the mode is on and the Dialogs ingress is configured
- **THEN** startup fails, naming Dialogs, the reason it cannot attribute a sender, and the setting to
  clear

#### Scenario: An unconfigured Dialogs ingress does not refuse

- **WHEN** the mode is on and the Dialogs ingress is not configured
- **THEN** startup proceeds

#### Scenario: Several owner-minting ingresses are all named

- **WHEN** the mode is on and more than one owner-minting ingress is enabled
- **THEN** startup fails and every one of them is named, in one message

#### Scenario: The membership of the set is checked, not assumed

- **WHEN** the ingresses that mint the configured owner are enumerated
- **THEN** each one has a refusal in the mode's refusal set
- **AND** an ingress that gains that property without gaining a refusal fails the check

### Requirement: A producer acting for the instance is not refused as an ingress

The rule above SHALL apply only where a real sender exists to be attributed. A producer that acts on
the instance's own behalf, has no author in its input, and publishes a scope it declares synthetic
SHALL NOT be refused by it.

The distinction is the whole content of the rule, and losing it breaks the mode in the opposite
direction. An ingress that misattributes a person is a defect because a person was there to name. A
producer with no person cannot name one, says so at the point it builds the scope, and is already
governed by the separate requirement that such a producer publishes a visibly synthetic scope. Refusing
it as though it were an ingress would remove a working path to fix a defect it does not have; leaving
the boundary unstated invites exactly that, because both build the same ownership pair from the same
configuration.

#### Scenario: The in-process skill bridge is not refused by the ingress rule

- **WHEN** the mode is on and a producer that acts for the instance and declares its scope synthetic
  is present
- **THEN** the ingress refusal does not name it
- **AND** startup does not fail on account of it

#### Scenario: The reason a producer is exempt is recorded where the refusals are

- **WHEN** the mode's refusal set is read
- **THEN** it states that the rule covers ingresses with a sender to attribute, and not producers that
  have none

### Requirement: A refusal describes the configuration that will run

Every check the mode makes SHALL obtain its inputs from the same resolution the runtime composes
with, and SHALL NOT read the underlying setting keys a second time. A check with its own reader is a
second source of truth for one configuration, and the moment the two disagree the mode either accepts
something it should refuse or refuses a configuration nobody set — the second being the worse of the
two, because the deployment is then told to fix a value it never chose.

Where the shared resolution rejects a configuration outright rather than returning one, the mode
SHALL surface that rejection as a refusal carrying the resolver's reason. A configuration
contradiction is exactly what the mode exists to name, so it SHALL NOT leave startup as an
unhandled error from a helper.

#### Scenario: The numbered database keys are the configuration the check sees

- **WHEN** the mode is on and the runtime database is configured through the numbered failover keys,
  with the single legacy key unset
- **THEN** those URLs are what the start-up database refusal inspects
- **AND** startup proceeds rather than refusing a database that was never configured

#### Scenario: A refusal names the key that was consulted

- **WHEN** the mode is on, the numbered failover keys are the keys the runtime reads, and they carry
  a runtime database configuration the start-up refusal rejects
- **THEN** startup fails naming that database and the keys the runtime reads

#### Scenario: A configuration the resolver rejects becomes a refusal

- **WHEN** the mode is on and the runtime database configuration is one the shared resolution rejects
- **THEN** startup fails with the mode's refusal, carrying the reason the resolution gave
- **AND** no unhandled error from the resolution leaves startup

### Requirement: Instances of one deployment agree on one object-storage keyspace

The keyspace an instance will use SHALL be recorded once for the deployment in the runtime database:
the object-storage backend, its bucket, and the key prefix built from the tenant id and configured prefix.
Every later start under the mode SHALL compare its own resolved keyspace with
the recorded one and SHALL refuse on any difference, naming the recorded value, the value found, and
the deployment step that replaces the record.

The runtime database is where this can be enforced at all: a process cannot read its peers'
environment, and the start-up refusal has already rejected every runtime database that cannot span
processes, so it is the one substrate the instances are known to share. A marker in object storage
cannot serve —
it would sit inside the very prefix under test, so a divergent instance would write its marker into
its own private keyspace and never meet the other one.

The record SHALL be established by a write that decides in the statement, and a start that loses that
race SHALL compare against the row that won rather than overwriting it. The recorded keyspace SHALL
be replaced only by an explicit deployment step, never by a setting that suppresses the comparison: a
suppression switch on a safety refusal is left on.

This check SHALL be understood to report *disagreement*, not correctness. It cannot say which
instance is right — whichever started first defines the record — and a fleet that is uniformly wrong
agrees with itself and starts. Naming both values is what makes it useful, because the operator
comparing them is comparing two deployment manifests.

With the mode off, no keyspace SHALL be recorded and none SHALL be compared, so the mode's default
stays the behaviour that shipped.

#### Scenario: The first start records the keyspace

- **WHEN** the mode is on and no keyspace is recorded for the deployment
- **THEN** the instance records its resolved backend, bucket and key prefix
- **AND** startup proceeds

#### Scenario: An instance with a different tenant id is refused

- **WHEN** the mode is on, a keyspace is recorded, and this instance's tenant id yields a different
  key prefix
- **THEN** startup fails, naming the recorded prefix, the prefix found, and the step that replaces
  the record

#### Scenario: An instance pointed at another bucket is refused

- **WHEN** the mode is on, a keyspace is recorded, and this instance resolves the same prefix in a
  different bucket
- **THEN** startup fails, naming both buckets

#### Scenario: Agreeing instances start

- **WHEN** the mode is on and an instance resolves the recorded backend, bucket and prefix
- **THEN** startup proceeds and nothing is rewritten

#### Scenario: Two first starts race

- **WHEN** two instances start against a deployment with no recorded keyspace at the same time
- **THEN** exactly one record exists afterwards
- **AND** the instance that did not write compares against the record that won

#### Scenario: The schema check speaks first

- **WHEN** the mode is on and the runtime database schema is below the version the runtime requires
- **THEN** startup fails as a schema failure naming the upgrade step
- **AND** no keyspace comparison is attempted or reported

#### Scenario: The mode off records nothing

- **WHEN** the mode is off
- **THEN** no keyspace is recorded and no comparison is made

### Requirement: Every process serving the mode makes the same refusals

The refusals SHALL be a property of the mode rather than of one entry point: they SHALL be declared once,
and every composition root that serves owners under the mode — or that reads or writes owners' durable
state on their behalf without serving them directly — SHALL run the whole set before it does any of that
work. No entry point SHALL re-implement a subset of them.

"The whole set" SHALL include both phases: the refusals that can be made from settings alone, and the
comparison against what the deployment recorded, which needs the store open and the schema at head. A
root that runs only the first phase has run a subset. A tier that never mounts an owner-facing surface
still writes an owner's artifacts into the deployment's keyspace, so a divergent keyspace in that tier
produces the same missing-attachment failure as a divergent keyspace in a transport, with nothing at all
to say so.

Where several roots delegate their start-up to a shared helper, that helper SHALL run the whole set: a
helper that performs a subset makes every root delegating to it a root that checks less, and it does so
invisibly, because each of those roots appears to have delegated the whole thing.

A deployment that scales a transport into its own process gets a second composition root, and a second
composition root that checks less is a way back into exactly the configuration the mode refuses — this
time in the process that writes the uploads.

#### Scenario: A second entry point refuses what the first would

- **WHEN** a process other than the main server entry point starts under the mode with a
  configuration the mode refuses
- **THEN** it fails to start with the same refusal, before it serves anything

#### Scenario: The refusal set is declared once

- **WHEN** the startup paths are inspected
- **THEN** exactly one module declares what the mode refuses
- **AND** no entry point carries a check of its own that duplicates or narrows that set

#### Scenario: A tier that writes owner data compares the keyspace too

- **WHEN** a process that runs no owner-facing surface but writes owners' objects starts under the mode
  against a deployment whose recorded keyspace differs from the one it resolves
- **THEN** it fails to start, naming the recorded value and the value it found
- **AND** it fails in the same way a process serving an owner-facing surface would

#### Scenario: A shared start-up helper runs the whole set

- **WHEN** the helper that several roots delegate their start-up to is inspected
- **THEN** it performs both phases of the mode's checks
- **AND** no root that delegates to it adds a phase the helper omitted

### Requirement: Every tier of the mode is named, with the entrypoint that starts it

The processes a deployment of the mode runs SHALL be enumerated in one place, and each entry SHALL name
the module that starts that tier. A tier that exists only as a package is not deployable; a tier named
only in prose drifts from the module that runs, and the drift is discovered by an operator whose
deployment does not come up.

The enumeration SHALL be checked against the tree rather than asserted: for every named tier, the named
entrypoint SHALL be importable and SHALL expose the callable a deployment invokes. Adding a tier without
an entrypoint, or moving an entrypoint without updating the enumeration, SHALL fail that check.

The enumeration SHALL state the start command in the form the deployed image can run. Where an image
installs its dependencies without installing the project, no console script it declares exists inside
that image, so a start command naming one is a command that fails at deploy time and nowhere earlier.

The enumeration counts processes, not surfaces. A surface added to the mode SHALL join the composition
root that already composes what it needs and SHALL add no entry here: the chat registry's router is
included by `multiuser_config/config_registry/app.py` beside the agent registry's and the owner
settings writer's, over the one engine that root already builds, exactly as `server.py` includes all
three in the single-process mode. A root of its own would compose the same settings, the same
refusals, the same store and the same engine to answer one more table, and would add a process an
operator has to run, watch and scale for no separable reason.

Because the entry no longer says what the tier answers for, each entry SHALL name the owner-facing
paths its tier serves, and adding a router to an existing root SHALL update that entry. This is the
one artifact where the mode's shape is written down, and a surface that appears in it nowhere is one
whose absence from the address that must route it — under the requirement that a deployment is
reachable at one address — is noticed by nobody until a request for it reaches a transport instance
that does not serve it.

A tier serving several surfaces SHALL NOT be named in the enumeration for one of them. The entry
keyed `agent-registry` already starts `multiuser_config.config_registry`, which serves the agent
registry and the owner settings writer both; a third router makes that name describe a third of what
the process does, and the name is what an operator reads in the deployment's own output when
deciding which process to look at. The name SHALL be the root it starts.

#### Scenario: Every named tier has an entrypoint that exists

- **WHEN** the enumeration of the mode's tiers is read
- **THEN** each entry names a module that can be imported
- **AND** each names the callable a deployment invokes to start that tier

#### Scenario: A tier without an entrypoint fails the check

- **WHEN** a tier is added to the enumeration and no importable entrypoint is named for it
- **THEN** the check fails, naming that tier

#### Scenario: The start command is one the deployed image can run

- **WHEN** a tier's start command is read for a deployment
- **THEN** it does not depend on a console script the image does not install
- **AND** the same command starts the tier from a source checkout

#### Scenario: A surface added to an existing root adds no tier

- **WHEN** the chat registry's router is added to the mode
- **THEN** the enumeration names the same number of tiers as before
- **AND** the router is served by the root that already includes the agent registry and the owner
  settings writer

#### Scenario: Each entry names the paths its tier serves

- **WHEN** the enumeration is read against the deployment's address configuration
- **THEN** every owner-facing path an entry names is routed to that tier
- **AND** a path a tier mounts that no entry names fails the comparison

#### Scenario: A tier serving three surfaces is not named for one

- **WHEN** the name the enumeration gives the tier that serves the agent registry, the owner settings
  writer and the chat registry is read
- **THEN** it names the composition root that starts it rather than one of the three surfaces

### Requirement: Tiers of one deployment agree on their shared configuration from one definition

The configuration of a multi-tier deployment SHALL be split, in one declared place, into the values every
tier of one deployment shares and the values that belong to a single tier. Shared values SHALL be
supplied to every tier from one definition rather than repeated per tier, so that "these processes are
one deployment" is a property of the deployment description and not of an operator's care in copying.

The declaration SHALL state, for each shared value, whether a cross-process comparison stands behind it.
A value with a comparison behind it refuses a start on disagreement. A value with none fails silently.
The message broker's whole key set has none: the backend selector, the Redis address and key prefix,
and the Kafka settings. A tier pointed
at another broker, or selecting another backend, reads an empty queue, publishes into a queue nobody
reads, and reports nothing wrong. Until such a comparison exists, the deployment description is the
only thing preventing that split, so overriding one tier's copy of a shared value SHALL be detectable
in the description itself.

Kafka consumer group ids are global to a cluster rather than scoped to a topic. Two deployments that
share one Kafka cluster SHALL therefore be kept apart by their topic prefix, which also prefixes every
group id.

A deployment whose broker is Kafka SHALL still declare its Redis address and key prefix as shared values
when the owner-settings cache is shared through Redis, because that cache is a key/value store Kafka does
not provide.

A per-tier value SHALL be one that changes only how much that tier does, never what an owner's data
means. How many runs a tier holds is its own; the runtime database, the object-storage keyspace, the
message broker and the declaration of the mode are not.

The mode's excluded ingresses SHALL appear in no tier's configuration. A configuration that the mode
refuses is not made acceptable by being set on the one tier that does not compose that ingress.

#### Scenario: A shared value is supplied from one definition

- **WHEN** the deployment description of a multi-tier deployment is read
- **THEN** each value the tiers must agree on is defined once and applied to every tier
- **AND** no tier restates a shared value with a value of its own

#### Scenario: A per-tier value is not in the shared set

- **WHEN** a value that sizes one tier is declared
- **THEN** it is declared on that tier and not in the shared set

#### Scenario: A shared value with no comparison behind it is declared as such

- **WHEN** the shared configuration is read
- **THEN** each shared value states whether a cross-process comparison refuses a disagreement
- **AND** the values with no such comparison are named as relying on the description alone

#### Scenario: Every broker key is a shared value

- **WHEN** the shared set of a deployment description is read
- **THEN** it carries the broker backend selector, the Redis address and key prefix, and every Kafka
  setting a tier reads to reach the broker or to name its topics and groups
- **AND** the declaration names tiers on two brokers, each reading an empty queue, as a split that
  nothing detects

#### Scenario: An excluded ingress appears in no tier

- **WHEN** every tier's configuration is inspected
- **THEN** none of them enables an ingress the mode refuses

### Requirement: A deployment of the mode is reachable at one address

A deployment puts exactly one address in front of the transport instances. Balancing with no sticky session is safe because every transport instance subscribes to the outbox in broadcast mode and delivers only what its own connections hold.

### Requirement: No skill that executes code is exposed in the mode

With the mode on, the common skill surface SHALL withhold every skill whose manifest declares a type
this runtime executes — `script`, `extension`, `tool`. A withheld skill SHALL NOT be loaded or exposed:
it reaches no prompt index, no skill read, no tool provider, and no execution path. Filtering SHALL
happen in the common surface rather than in one provider, so every current or future source inherits
the property without repeating it.

The filter SHALL name the types it withholds rather than the types it keeps: a package delivered by
an external hub declares a type this runtime does not know, executes nothing, and SHALL be eligible
for the documentation-skill surface like any other non-executable package.

The filter is what makes the mode's support for documentation skills usable on a deployment whose
image also ships executable ones. A deployment SHALL NOT have to mask its skills tree to start,
because masking the tree removes the supported skills together with the unsupported ones.

An executable skill withheld by this filter is withheld for the whole process lifetime, not per task:
the mode is captured in the immutable deployment availability at composition, and no task-level input
or `OUROBOROS_USER_ENABLED_SKILL_IDS` value can widen it. The owner-and-agent allowlist SHALL be
applied only below this deployment ceiling and SHALL expose only named documentation skills to one
task.

The agent-runtime composition root SHALL initialize the process-wide skill catalog before the tool
catalog and SHALL give its deployment `SkillSurface` to both skill-backed tool providers. Both
catalogs SHALL remain fixed after composition, and the task path SHALL NOT refresh either one. The
tier SHALL NOT add extension loading or a `SkillBridge`; those remain absent from this tier.

#### Scenario: Only enabled documentation skills in a mixed tree are exposed

- **WHEN** the mode is on and the discovery root holds both documentation skills and skills whose
  type executes code
- **AND** the owner allowlist names one documentation skill
- **THEN** the task surface returns only that permitted documentation skill
- **AND** the executable ones appear in no prompt index, skill read, tool provider, or summary

#### Scenario: A package whose type this runtime does not know can be selected

- **WHEN** the mode is on and the discovery root holds a package whose manifest declares a type
  outside this runtime's own set
- **AND** its canonical id is in the owner allowlist
- **THEN** the common task surface returns it, and the task can read it
- **AND** no execution path accepts it, in this mode or outside it

#### Scenario: Naming an executable skill grants nothing

- **WHEN** an owner allowlist names a script, tool, or extension skill in multi-user mode
- **THEN** the deployment and task surfaces omit it
- **AND** direct lookup answers as if that skill does not exist

#### Scenario: Skill availability outside the mode is unchanged

- **WHEN** the mode is off
- **THEN** this owner-selection change does not alter the existing executable-skill authorization
- **AND** every skill the single-user deployment previously exposed remains governed by its existing
  deployment and execution paths

### Requirement: The skill-execution tools are not composed in the mode

With multi-user mode on, the repo-defined provider SHALL omit `skill_exec`. Runtime provisioning
and repair tools do not exist. Executable packages from every skill provider SHALL be withheld by
the common skill surface until multi-user sandbox execution is implemented.

#### Scenario: A future provider publishes an executable package

- **WHEN** multi-user mode is on and any provider publishes a script, tool, or extension skill
- **THEN** the deployment and task surfaces omit it
- **AND** no provider-specific omission is required for safety

### Requirement: No egress authorization kind is refused under the mode

The mode's declared refusal set SHALL NOT refuse an MCP entry or an A2A remote-agent entry for the
authorization kind it declares. The security context a `user_headers` call reads and the personal token
a `user_token` call reads are all stored in the runtime database every process of the mode shares —
personal MCP tokens in `mcp_user_tokens`, personal A2A tokens in `a2a_user_tokens` — so the agent tier
that makes a call reads what the browser transport tier or a writer stored, whichever replica makes the
call; `none` and `mtls` add nothing to a call. An A2A task handle is held by no process and no store: it
is computed from `OUROBOROS_CHAT_ENCRYPTION_KEY`, which every tier of the deployment shares, so a handle
one agent-tier replica reported opens on every other, and a task started through one replica is
followed through another. An entry of any kind SHALL remain subject to the mode's other refusals, such
as the instance-local address refusal and the refusal of a `user_headers` name the header collector does
not declare, for MCP and A2A alike.

#### Scenario: Every authorization kind starts under the mode

- **WHEN** the mode is on, the MCP kill switch is on, and an enabled entry at a shared address declares
  `none`, `user_headers`, `user_token` or `mtls`
- **THEN** no refusal of the mode names that entry or user authorization

#### Scenario: Every authorization kind of an A2A agent starts under the mode

- **WHEN** the mode is on, A2A egress is on, and an enabled remote-agent entry at a shared address
  declares `none`, `user_headers` forwarding only declared names, `user_token` or `mtls`
- **THEN** no refusal of the mode names that entry, A2A egress or user authorization

#### Scenario: A personal A2A token one tier stored authorizes the call another tier makes

- **WHEN** the owner settings writer stores a personal token for owner `o`, agent `a` and remote agent
  `r` in the runtime database, and a browser-originated run of `o` and `a` in an agent-tier process calls
  `r`, whose entry declares `user_token`
- **THEN** every request of that call carries the stored token

#### Scenario: A task started through one replica is followed through another

- **WHEN** two agent-tier processes compose over the same `OUROBOROS_CHAT_ENCRYPTION_KEY`, and a task
  started through one of them by owner `o` and agent `a` with remote agent `r` reported a handle
- **THEN** `a2a_task_status` with that handle, called by a run of `o` and `a` in the other process, is
  answered for that task
- **AND** opening the handle reads no store

### Requirement: An unsupportable configuration for several owners is refused at startup

Startup SHALL validate the whole configuration against the mode and SHALL refuse to start on a
contradiction, rather than starting and failing later in a way that resembles something else. Each
refusal SHALL name what was found, why it cannot work, and what to change.

The mode SHALL refuse at least: an enabled ingress that mints the configured owner for every caller,
an enabled external tool server or outbound A2A agent that the instances of the deployment cannot
share, and an enabled MCP server or outbound A2A agent that forwards a header or a cookie the header
collector declaration does not declare.

The runtime database is absent from that list because it is refused before the mode is consulted: a
runtime database configuration that is absent, or that names any dialect other than PostgreSQL, is
refused at start-up in every mode, so a database that permits one writer cannot be configured at all.
The mode SHALL inherit that refusal rather than repeating it, and SHALL NOT make a database check of
its own. A refusal the mode does not restate is one fewer place where the two can disagree about the
same configuration.

Outbound A2A egress SHALL NOT be refused for being enabled. Every request of an A2A operation carries
the credentials of the run that made it, and every task id the model holds is bound to the owner and
agent that started the task (`a2a-egress`), so a deployment serving several owners delegates without
one owner's call leaving under another owner's credentials or following another owner's task. An A2A
remote-agent entry SHALL instead be subject to the same address refusal and the same undeclared-name
refusal as an MCP entry, examined only while `A2A_EGRESS_ENABLED` is on and only for enabled entries.
Each A2A refusal SHALL name `security_config/a2a.py` as the list to change, separately from any
refusal naming `security_config/mcp.py`.

A skills tree that carries skills whose type executes code SHALL NOT be refused. What a deployment's
image ships is not a contradiction the mode can only survive by not starting: the mode reads the
part of that tree it supports and leaves the rest unloaded, which is stated as its own requirement.

An external tool server or outbound A2A agent the mode reaches SHALL be one peer for the whole
deployment, reached over the network. A configuration under which each instance reaches a *different*
peer, or none, while naming one SHALL be refused, and the refusal SHALL name each such entry.
The child-process form of that defect is **unrepresentable** rather than refused: an MCP entry declares
a network transport and an address, an A2A entry declares an address, and no transport starts the peer
as a child of the process that talks to it. The **address** form survives it and is what the refusal
covers — an address that resolves inside whichever instance asks names a different peer per replica,
so the deployment multiplies one configured server or agent by its replica count, silently and under a
name that says one. For an A2A agent the same address also strands delegated work: a follow-up that
another replica serves reaches a peer that never received the task.

Object storage is absent from that list for the same reason the runtime database is: the only
object-storage backend the runtime carries is the shared one, and no setting selects another, so
storage that only the writing instance can read cannot be configured at all. A missing bucket is
refused before the mode is consulted, in every mode and every deploy environment
(`object-storage-addressing`). The mode SHALL inherit that refusal rather than repeating it, and SHALL
NOT make a bucket check of its own.

Where several instances of one problem exist, the refusal SHALL list them rather than stopping at the
first, because a deployment fixing them wants the whole list.

#### Scenario: A single-writer database is refused regardless of the mode

- **WHEN** the runtime database configuration is absent, or names a dialect other than PostgreSQL
- **THEN** startup fails, naming what was found, the reason, and the setting to change
- **AND** it fails the same way whether the mode is on or off
- **AND** no refusal of it is attributed to the mode

#### Scenario: An owner-minting ingress is refused

- **WHEN** the mode is on and an ingress that mints the configured owner for every caller is enabled
- **THEN** startup fails, naming that ingress

#### Scenario: A skills tree carrying executable skills is not a refusal

- **WHEN** the mode is on and the discovery root carries skills whose type executes code
- **THEN** startup proceeds
- **AND** no refusal names any skill

#### Scenario: A tool server that cannot be shared is refused

- **WHEN** the mode is on, the tool kill switch is on, and an enabled MCP entry names an address that
  resolves inside whichever instance asks
- **THEN** startup fails, naming that entry and its address
- **AND** every such entry is named, not only the first
- **AND** every spelling of that address is covered — the loopback literal, the name that
  resolves into it, and the unspecified address a service binds to

#### Scenario: A shared tool server starts

- **WHEN** the mode is on and every enabled MCP entry names an address each instance resolves to the
  same server
- **THEN** startup proceeds

#### Scenario: An outbound A2A agent that cannot be shared is refused

- **WHEN** the mode and A2A egress are on, and an enabled remote-agent entry names an address that
  resolves inside whichever instance asks
- **THEN** startup fails, naming that entry and its address without rendering URL credentials
- **AND** every such entry is named, not only the first, and every spelling of that address is covered —
  the loopback literal, the name that resolves into it, and the unspecified address a service binds to
- **AND** the refusal names `security_config/a2a.py` and `A2A_EGRESS_ENABLED=false` as what to change
- **AND** disabled entries and the complete list behind an off kill switch remain unexamined

#### Scenario: An outbound A2A agent forwarding an undeclared name is refused

- **WHEN** the mode and A2A egress are on, the header collector declaration names header
  `X-Sudir-Token`, and an enabled remote-agent entry `hr` declares `user_headers` with header `X-Other`
- **THEN** startup fails with a refusal naming `hr header:x-other` and `security_config/a2a.py`
- **AND** the refusal renders no URL and no token
- **AND** a disabled entry, and the complete list behind `A2A_EGRESS_ENABLED` off, remain unexamined

#### Scenario: Outbound A2A egress at a shared address with declared names starts

- **WHEN** the mode and A2A egress are on, and every enabled remote-agent entry names an address each
  instance resolves to the same agent and forwards only names the header collector declares, whatever
  authorization kind it declares
- **THEN** startup proceeds
- **AND** no refusal names A2A egress, `A2A_EGRESS_ENABLED` or any remote-agent entry

#### Scenario: The kill switch off leaves the tool list unexamined

- **WHEN** the mode is on, the tool kill switch is off, and an enabled MCP entry names an
  instance-local address
- **THEN** startup proceeds, because no process will connect to it

#### Scenario: A supported configuration starts

- **WHEN** the mode is on with a runtime database the start-up refusal accepted, shared object storage
  with a bucket, and no excluded ingress
- **THEN** startup proceeds

#### Scenario: A missing bucket is refused regardless of the mode

- **WHEN** no `OUROBOROS_S3_BUCKET` is configured
- **THEN** startup fails, naming `OUROBOROS_S3_BUCKET`
- **AND** it fails the same way whether the mode is on or off
- **AND** no refusal of it is attributed to the mode

### Requirement: No durable state an owner produced reaches an instance's local disk

Under the mode, every durable object produced on an owner's behalf SHALL be written to the shared
object-storage backend. A storage scope that the composed storage provider serves from the answering
instance's own filesystem SHALL have no writer — not on the task path, not on the runtime path — and
a call that would create a durable object in such a scope SHALL be refused, naming the scope, rather
than succeeding. A durable file written straight to the instance's filesystem, beside the provider,
falls under the same rule: the shared backend never holds it, so no other instance reads it back,
and no runtime decision SHALL be made conditional on writing one.

This requirement covers durable state only. A lock file, a temporary path, and a per-task workspace
deleted when the task ends are not durable state, and the instance's data directory SHALL remain
writable for them. A requirement that forbade every local write would be false at the first lock and
would be enforced as though it were decorative.

What a deployment reports about its storage SHALL name the backend that actually holds durable
objects, and SHALL NOT name a local tier that no writer reaches. An operator reads that name to
decide what to provision and what to back up, so a name describing a routing split that carries
nothing asks for a volume nobody needs.

#### Scenario: A runtime path leaves no marker on the instance it ran on

- **WHEN** an owner asks the runtime to restart
- **THEN** no marker file is left under the instance's data directory
- **AND** the restart is not made conditional on writing one, since a file written outside the
  storage provider is read back by no other instance

#### Scenario: The reported storage backend names no local tier

- **WHEN** the resolved storage backend is reported
- **THEN** the reported name is the shared backend alone
- **AND** it names no local split

#### Scenario: A transient working file is not durable state

- **WHEN** a task creates its temporary workspace, or a helper takes a lock file, under the
  instance's data directory
- **THEN** the requirement does not refuse it
- **AND** the workspace is removed when the task ends

