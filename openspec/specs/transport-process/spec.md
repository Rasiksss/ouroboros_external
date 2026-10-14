# transport-process Specification

## Purpose

The transport tier deployed as its own process: a second composition root under
`multiuser_config/transport/browser/` that serves the browser surface — the same route collection and
WebSocket hub the full runtime mounts — while starting no supervisor loop and no agent runtime. It
covers what such a process declares about itself, what it composes before it serves, how its
readiness probe and port binding behave, and the boundary it does not cross into the core.

## Requirements

### Requirement: An entrypoint declares which tier it serves

The repository SHALL provide an entrypoint whose composition is transport-only, beside the existing
full-runtime one, so that the browser transport can be deployed as its own process. It SHALL live in
`multiuser_config/transport/browser/` and be reachable as its own console script.

It SHALL compose exactly what serving a browser client needs: the gateway route set, the browser
identity resolver, the agent registry that resolver resolves the agent axis through, the runtime
metadata store, the drive root, and the message gateway. It SHALL start no supervisor loop and no
agent runtime, and no module it imports SHALL name `supervisor` or `ouroboros.agent_runtime`.

A tier that is not declared is inferred, and today the inference is "does this environment hold LLM
credentials" — which is how a process with no supervisor in it can still be treated as one that has
started. What a process is SHALL be a statement it makes, not a shape read off its configuration.

The message gateway it composes SHALL be the backend the configuration names, resolved at the
composition boundary rather than chosen at each root. A named backend that cannot be built — a
broker-backed backend with no broker address, for instance — SHALL fail the process at startup naming
the settings that disagree, and the root SHALL NOT fall back to another backend. A fallback is
indistinguishable from success at startup and shows up later as a deployment of N instances each
holding its own queue: submissions no supervisor drains, and answers that reach the connections of one
process only. The default SHALL keep an unconfigured deployment on the in-process backend, so that
composing a broker is a decision rather than an accident.

The browser transport SHALL NOT be forked to achieve this. Both entrypoints SHALL mount the same
browser route collection and drive the same WebSocket hub, so a change to the surface lands once.

The transport tier SHALL mount that collection and nothing beside it. The full runtime additionally
mounts the agent registry's own surface, which in this mode belongs to a composition root of its own:
a route set the transport tier served would be a surface owned by two tiers at once. The identity of
the two route sets SHALL therefore be asserted of the shared browser collection, and the registry
surface SHALL sit beside that collection rather than inside it.

#### Scenario: The transport process starts no core threads

- **WHEN** the transport entrypoint has finished starting
- **THEN** no supervisor loop and no agent runtime is running in the process
- **AND** nothing it imported names `supervisor` or `ouroboros.agent_runtime`

#### Scenario: Both entrypoints mount the same browser surface

- **WHEN** the route set the transport entrypoint mounts is compared with the shared browser route
  collection the full runtime mounts with browser ingress enabled
- **THEN** the two are identical, and both come from that shared collection

#### Scenario: The transport tier serves no registry route

- **WHEN** the route set the transport entrypoint mounts is inspected
- **THEN** it contains no agent-registry route
- **AND** the full runtime's additional registry mount sits beside the shared collection rather than
  inside it

#### Scenario: The process names what it composed

- **WHEN** the transport entrypoint starts
- **THEN** it logs the tier it serves and which message-gateway backend it composed
- **AND** the backend it names is the one the configuration selected, not a fixed string

#### Scenario: A named backend that cannot be built stops the process

- **WHEN** the configuration selects a broker-backed gateway and the setting carrying the broker
  address is empty
- **THEN** startup fails naming both settings
- **AND** no gateway is composed and no route is served

#### Scenario: An unconfigured deployment keeps the in-process backend

- **WHEN** the transport entrypoint starts with no backend selected
- **THEN** it composes the in-process backend and says so
- **AND** the full-runtime entrypoint is unaffected by the selector

### Requirement: The transport root composes every process-wide holder before it serves

The transport entrypoint SHALL compose the runtime metadata store, the identity resolver and the
message gateway before the first request can be served, and SHALL run the multi-owner mode's
configuration validation exactly as the full runtime does. A refusal that only one entrypoint
performs is not a refusal.

An uncomposed holder SHALL fail the process at startup rather than at the first request that reaches
it: the runtime store is the one holder whose absence turns a transport-only process into a
half-working one, and the failure would otherwise arrive as a request-time error on a route that
looks unrelated.

The process SHALL verify that the database schema is at or above the revision this build needs and
SHALL NOT bring the schema up itself. Bringing it up is a deployment step; several instances
starting together would otherwise race one another through the migrations, and one of them would be
changing the schema under peers already serving it. The verification is read-only, so any number of
instances MAY perform it concurrently.

#### Scenario: A missing holder stops the process, not a request

- **WHEN** the entrypoint is started with a composition step omitted
- **THEN** startup fails naming the holder that was not composed
- **AND** no route is served

#### Scenario: A schema below the build's is refused

- **WHEN** the transport process starts against a database below the revision the build needs
- **THEN** startup fails, naming the stored revision, the required one, and the command that brings
  the schema up

#### Scenario: Several instances start against one database

- **WHEN** several transport instances start concurrently against a database already at head
- **THEN** each verifies the schema without writing to it, and all of them start

#### Scenario: The mode's refusals run in this process too

- **WHEN** the transport entrypoint starts with the multi-owner mode declared and a configuration
  the mode refuses
- **THEN** startup fails with the same refusal the full runtime produces

### Requirement: A tier that names the Kafka broker describes its connection before it records or serves anything

A composition root that composes a broker seam SHALL, when its configuration selects the Kafka broker,
describe the Kafka connection from settings as one step of its start-up. The step SHALL run after the
mode's refusals and before the runtime metadata store is opened, so that a refused broker configuration
never records the deployment keyspace and never serves a route. The transport root, which composes
inline, SHALL take the step at the same position as the shared component start-up. A root that composes
no broker seam SHALL NOT take it, because its availability would then depend on a broker it never uses.

The step SHALL refuse the start, naming the setting key concerned, when the bootstrap servers are empty
or the broker protocol version is missing or cannot be parsed. It SHALL make no other check of the
connection's settings: what it does not refuse surfaces from the client library when a seam first uses the
broker (capability `kafka-connection`). No refusal SHALL contain a server address or a credential.

The step SHALL NOT contact the broker. It SHALL NOT check that a topic exists, compare a topic's
configuration or read a consumer group's positions, because the topics are the infrastructure's
(capability `kafka-topic-provisioning`). A broker that does not answer at start, or a topic that is
missing, SHALL therefore not refuse the start. The tier starts, and the readers and publishers that use
the seams meet either the way they meet a broker lost after start. Neither SHALL be a process exit.

Until the process's lifespan starts the readers and publishers that use the seams, nothing SHALL contact
the broker. Composing a seam over the Kafka broker SHALL build no client and perform no broker I/O, and
SHALL use the connection the step described rather than reading the settings a second time. Composing
the seams over another backend SHALL NOT import the Kafka client library, the Kafka connection
description or a Kafka backend module. The pre-existing start-up announcement and tracing producers keep
their own settings and their own imports; this requirement does not govern them.

#### Scenario: Empty bootstrap servers are refused naming both settings

- **WHEN** a tier starts with the Kafka broker selected and no bootstrap servers configured
- **THEN** startup fails naming the backend selector and the bootstrap-servers setting
- **AND** no runtime metadata store is opened and no route is served

#### Scenario: A missing protocol version is refused

- **WHEN** a tier starts with the Kafka broker selected and no broker protocol version configured
- **THEN** startup fails naming the protocol-version setting
- **AND** no client probes the broker for its version

#### Scenario: The start contacts no broker and checks no topic

- **WHEN** a tier starts with the Kafka broker selected and settings that describe a connection, while
  nothing answers at the bootstrap servers
- **THEN** the broker step completes without building a Kafka client or opening a connection
- **AND** the start is not refused over a topic, a consumer group or the broker's reachability

#### Scenario: A broker lost after start is an outage, not an exit

- **WHEN** the broker becomes unreachable after a tier has started
- **THEN** the tier keeps running, and its readers record the outage and resume when the broker returns

#### Scenario: Composing the Kafka seams performs no broker I/O

- **WHEN** a composition root composes the message gateway and both event seams over the Kafka broker
  once the start-up step has described the connection
- **THEN** no producer or consumer is constructed and no connection is opened

#### Scenario: Composition on another backend imports no Kafka module

- **WHEN** the composition module and the composition roots are inspected
- **THEN** the Kafka client library, the Kafka connection description and every Kafka backend
  module are imported only inside the branch that composes the Kafka broker
- **AND** no composition root imports any of them at module scope

### Requirement: A readiness probe answers about the process answering it

`/ready` SHALL report only on conditions the answering process can observe in itself. It
SHALL NOT require a signal that only a co-located component sets, and it SHALL NOT report ready by
virtue of a component being absent from its configuration.

Both failure modes exist today and are opposites of each other: a transport image built with model
credentials would wait forever for a bridge only a local supervisor thread publishes, so the load
balancer admits nobody; the same image built without credentials would answer ready while the
supervisor fleet is entirely down.

A claim about another tier SHALL rest on a signal that crosses the tier boundary. No such signal
exists in this change, so the transport process's readiness SHALL NOT assert that a supervisor is
available, and the probe's documented meaning SHALL state that it does not.

The runtime database SHALL be a condition the process observes in itself rather than a peer it
attests: every composition root opens a connection to it before it serves and none of them works
without it. Readiness SHALL therefore report whether the answering process reaches that database,
over a connection held for the probe alone rather than over the pool the runs work through. How full
that pool is asks about load, not about the database: an instance whose pool is momentarily full is
the busiest one, and removing it from the address sheds no work, because nothing about readiness
stops this process claiming more.

The probe SHALL dial a connection of its own, SHALL reach the same database through the same
failover set the store uses, and SHALL hold one bound over that whole dial — the connection, the
handshake, whatever the driver asks a connection it has just opened, and the reply — because no
timeout the driver offers ends any of them: a statement timeout runs inside the backend that owes
the reply and every other bound measures a peer that has stopped answering rather than one that
answers everything except the query. A deployment of one node or of a pair SHALL therefore answer
with a refusal rather than with a timeout, and no probe SHALL outlive the waits it declared.
A dial that passes its bound SHALL end in the probe's answer, and a connection the dial was handed
SHALL be closed outside that answer: closing it is what ends the wait, while waiting for the close,
like waiting for a cancellation the peer has to acknowledge, waits on the peer that has just missed
the bound. A connection so closed SHALL NOT be used again, no connection SHALL be kept for the probe
that follows, and a peer that holds connections open and answers nothing SHALL NOT cost this process
one connection per probe for the length of the outage. A dial that missed the bound before it was
handed one SHALL be cancelled and SHALL NOT be waited on either, there being nothing yet to close; it
is bounded instead by being the one exchange a process has once, before any connection of its own
exists.

The probe SHALL walk that failover set at most once per probe and SHALL NOT cut the walk short: a
longer set can cost an orchestrator the reason for a failure, never a probe that does not return,
whereas a probe that stopped at the first node would report a database its own store is serving from
as unreachable. The answer SHALL be read at the moment the probe is answered and SHALL NOT be
remembered between probes, so a database that returns makes the next probe ready with no restart and
no other signal.

A readiness failure SHALL name the condition and nothing about the connection: the driver's own
message carries the host, the port, the user and the database it dialled, and this probe is
unauthenticated.

Liveness SHALL NOT consult the database at all, and readiness SHALL NOT block the event loop that
answers the probe for longer than the check's own bound. An unreachable database is a condition every
instance recovers from unaided, while a restart costs the runs an instance holds; and a probe that
blocked its loop would stall the liveness answers queued behind it, turning one database outage into
a fleet-wide restart.

A startup probe SHALL answer whether the start is over at its own path, so that an orchestrator can
ask it with the patience a start deserves and stop asking once it has passed. It SHALL NOT repeat a
failure readiness reports: a start that ended in a failed loop is a start that is over, and the
replacement is liveness's to cause within its own threshold rather than the startup budget's.

This requirement governs the probe rather than one tier, and its consequence follows from what the
answering process composed. A process that composes no supervisor loop SHALL be ready once its own
composition is complete and its database answers, and SHALL claim nothing further. A process that
*does* compose the supervisor loop SHALL report that loop's own readiness and its own reported
failure, because for that process those are conditions observed in itself rather than claims about a
peer — and it SHALL still assert nothing about any tier it does not host. Every composition root
SHALL publish one probes object — startup, readiness and liveness behind one interface — that
describes what it actually composed, and the root SHALL say so where it installs it.

Liveness SHALL report only the vital parts of the answering process, and which parts are vital is the
composition root's to say. A process that hosts no loop has nothing to report beyond answering, and
SHALL publish no liveness report: its liveness stays unconditional. A process that hosts a loop
SHALL report that loop once the start is over, because a loop that died or stopped ticking is a
process an orchestrator has to replace, and an orchestrator replaces on liveness. Before that
liveness SHALL answer success: that window belongs to the startup probe, and a liveness that failed
there would restart every slow start.

A process whose main thread is a loop rather than a server SHALL serve the same three probes beside
that loop, on the port it is configured with, and stop them once the loop's exit is complete. A tier
that could not serve them SHALL run no loop at all: an instance an orchestrator cannot probe would
hold work nothing can tell apart from a wedged instance's. Its liveness SHALL report that loop's
heartbeat against a stall bound derived from a figure the loop already runs under, never a bound
stated for the probe alone.

#### Scenario: A transport process with model credentials becomes ready

- **WHEN** a transport process starts in an environment that carries model provider credentials
- **THEN** it answers ready once its own composition is complete
- **AND** it never waits for a bridge published by a local supervisor thread

#### Scenario: Readiness does not attest another tier

- **WHEN** the transport process is ready and no supervisor is reachable
- **THEN** readiness still reports on this process only
- **AND** the probe's documentation states that it does not attest supervisor availability

#### Scenario: An unreachable database is not ready

- **WHEN** readiness is requested of a process whose runtime database refuses the connection that
  process holds for the probe
- **THEN** the probe answers not ready
- **AND** the body names neither the host, the port, the user nor the database
- **AND** liveness answers success, because a database outage is not a process to replace

#### Scenario: A database that takes the query and never answers is not ready

- **WHEN** readiness is requested of a process whose database accepts the probe's query over a
  connection that stays open and sends no reply
- **THEN** the probe answers not ready within its own bound
- **AND** the connection that never answered is closed after that answer rather than left open

#### Scenario: A database that opens the connection and never finishes it is not ready

- **WHEN** readiness is requested of a process whose database accepts the connection and then
  answers neither the statements the driver makes of a new connection nor the probe's query
- **THEN** the probe answers not ready within that same bound
- **AND** the probe waits on none of that dial after answering, and the database answering again
  makes the next probe ready without a restart

#### Scenario: A database that returns needs no restart

- **WHEN** the database answers again after a readiness probe reported it unreachable
- **THEN** the next readiness probe answers ready
- **AND** no restart, signal or other probe was required to get there

#### Scenario: Liveness of a process that hosts no loop stays unconditional

- **WHEN** the liveness probe is requested of the transport process at any point after it is listening
- **THEN** it answers success without consulting anything
- **AND** the startup probe answers success once its own composition is complete

#### Scenario: A process that hosts the loop reports the loop

- **WHEN** readiness is requested of a process whose own composition includes the supervisor loop, and
  that loop has not finished starting
- **THEN** the probe answers not ready
- **AND** once the loop signals ready the probe answers ready, without consulting any other tier

#### Scenario: A hosted loop that failed is reported, not hidden

- **WHEN** the supervisor loop hosted by the answering process reports a startup or runtime failure
- **THEN** readiness answers not ready and carries the reported failure
- **AND** liveness answers failure carrying the same message
- **AND** startup answers success: the start is over, and the replacement is liveness's

#### Scenario: A worker process serves its probes beside its loop

- **WHEN** the agent-runtime or background-memory process has composed itself
- **THEN** startup, readiness and liveness answer success on its configured port
- **AND** liveness answers failure once the loop has not begun an iteration within its stall bound
- **AND** the surface is gone once the loop has exited

### Requirement: A transport process binds its configured port or fails

The transport process SHALL bind the host and port it was configured with, and SHALL exit with a
failure when that port is unavailable. It SHALL NOT scan for a free port, SHALL NOT drift to a
neighbouring one, SHALL NOT publish the port it settled on to a shared file, and SHALL NOT terminate
whatever process holds a port it wanted.

The bind is uvicorn's and comes last rather than first: a process whose main thread is a server runs
its whole start-up before it learns the port is taken. What a refused start attached SHALL therefore
be released by the same shutdown a served one goes through.

Port drift is harmless when one instance owns its network namespace and lethal when several share
one: an instance lands on a peer's port, a shared unlocked port file names whichever wrote last, and
a cleanup pass aimed at "whoever holds the port" kills a healthy peer. An orchestrator that placed
the process already decided where it listens, and a process that fails fast is restarted; a process
that quietly listens elsewhere is not.

#### Scenario: A busy port fails the process

- **WHEN** the configured port is already in use
- **THEN** the process exits with a failure naming the host and port it could not bind
- **AND** it does not attempt another port

#### Scenario: No shared port file is written

- **WHEN** the transport process starts successfully
- **THEN** it writes no file naming the port for another process to read

#### Scenario: Shutdown kills nothing outside the process

- **WHEN** the transport process shuts down for any reason
- **THEN** it terminates no other process, and sweeps no port

### Requirement: A transport process carries no self-restart machinery

The transport process SHALL NOT expose hooks that restart or re-exec itself, and SHALL NOT carry
application state that no mounted route reads. A termination signal SHALL end the process after its
own shutdown, leaving the decision to start a replacement to whatever placed it.

A process that restarts itself fights the orchestrator that scales it: the replacement it spawns is
outside the orchestrator's accounting, and the exit code it invents means nothing to the scheduler.
State that no route reads is a contract nobody checks — it survives refactors of the code that used
to consume it and is copied into the next entrypoint by inertia.

#### Scenario: No restart hook is reachable

- **WHEN** the transport application's state and its wiring are inspected
- **THEN** no restart or re-exec hook is present
- **AND** the process exposes no in-band way to ask for its own replacement

#### Scenario: Application state carries only what a route reads

- **WHEN** the keys the transport application publishes on its state are compared with the keys the
  mounted routes read
- **THEN** the two sets are the same

#### Scenario: A termination signal ends the process

- **WHEN** the transport process receives a termination signal
- **THEN** it shuts its transports down and exits
- **AND** it starts no replacement of itself

### Requirement: The transport process reads the conversation id without crossing into the core

The value every transport stamps its rows with SHALL be obtained by the transport process through
the storage it already composed, and SHALL NOT be obtained by importing the supervisor. The value is
database-backed, so reading it is a storage call rather than a call into another tier, and the
transport package's rule that it never imports `supervisor` SHALL hold for the process that hosts
it as well as for the modules inside it.

Whether that value should become per-owner is a separate question and is deliberately not decided
here: today it resolves under the instance's configured owner, so every owner's rows carry one
shared value. This change SHALL NOT alter what is stamped.

#### Scenario: The value is read through storage

- **WHEN** the transport process resolves the conversation id at startup
- **THEN** it reads it through the runtime store it composed
- **AND** the read names no supervisor module

#### Scenario: The stamped value is unchanged

- **WHEN** a message is persisted by the transport process
- **THEN** it carries the same conversation id the full runtime would have stamped it with
