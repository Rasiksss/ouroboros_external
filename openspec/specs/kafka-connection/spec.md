# kafka-connection Specification

## Purpose

One module in the shared infrastructure package, `infra/kafka_client.py`, owns the configuration
and construction of every Kafka client a broker backend or topic provisioning uses. A composition
root parses the `OUROBOROS_KAFKA_*` settings once into an immutable connection value that validates
bootstrap servers, credentials, SSL paths, topic prefixes, replication and message-size limits, and a
pinned protocol version. Backends obtain clients only through a factory interface, so a backend runs
unchanged against a real broker or an in-repo double. The description sets every client option whose
library default changes delivery semantics, holds at most one producer per topic, bounds a close
flush, isolates group membership from offset queries, applies one message-size ceiling across
producers, consumers and topics, floors the client library's loggers to suppress credentials, and
records outages from failed bounded requests rather than from bootstrap-connection state or empty
polls.
## Requirements
### Requirement: One description of the Kafka connection builds every Kafka client and refuses only what it cannot parse

The system SHALL form the configuration of every Kafka client a broker backend uses in one module of
the shared infrastructure package, `infra/kafka_client.py`, from the `OUROBOROS_KAFKA_*` settings
group, and nowhere else. A composition root SHALL parse the settings once into an immutable connection
value (`KafkaConnection.from_settings`). A backend and its constructor SHALL read no setting.

The connection SHALL refuse, before any client is built, only what it cannot turn into client options:
empty bootstrap servers, and a protocol version that is empty or not `MAJOR.MINOR`. A refusal SHALL name
the setting key and SHALL NOT contain a bootstrap server, a SASL user name or a password. It SHALL make
no other check: a malformed server entry, credentials the security protocol does not use, a TLS file that
cannot be read or a prefix Kafka does not admit surface from the client library when a client first
connects or first names a topic.

Topic names and consumer group ids SHALL be formed in one place from the prefix:
`KafkaConnection.topic(suffix)` returns `{prefix}.{suffix}`, and
`KafkaConnection.group_id(topic_suffix, group)` returns `{prefix}.{topic_suffix}.{group}`.

Importing the description SHALL load neither the Kafka client library nor the settings store. Only
the methods that build a client import the library, and the settings type is named for type checking
only. The bootstrap-server list parser the two pre-existing Kafka producers use lives in this module
too. Those producers, the start-up announcement and AEF tracing, keep their own settings keys.

#### Scenario: Empty bootstrap servers are refused naming both keys

- **WHEN** a connection is built from settings whose `OUROBOROS_KAFKA_BOOTSTRAP_SERVERS` is empty
- **THEN** a refusal names `OUROBOROS_KAFKA_BOOTSTRAP_SERVERS` and `OUROBOROS_BROKER_BACKEND`, as the
  Redis description's refusal of an empty URL names its address key and the selector
- **AND** no client is built

#### Scenario: A setting the connection does not check builds without a refusal

- **WHEN** a connection is built with a SASL user name under a security protocol that sends no credentials,
  a server entry without a port, or a prefix holding a character Kafka does not admit
- **THEN** the connection is built without a refusal

#### Scenario: The connection's representation carries no password

- **WHEN** a connection built with a SASL password is rendered with `repr` or `str`
- **THEN** the password does not appear

#### Scenario: Names are formed from the prefix

- **WHEN** the prefix is `fleet-a`, and the name formers are asked for the `inbound` topic and the `supervisor` group on it
- **THEN** the topic is `fleet-a.inbound`
- **AND** the group id is `fleet-a.inbound.supervisor`

#### Scenario: Importing the description loads no client and no settings store

- **WHEN** the description module is imported in a fresh process
- **THEN** neither the `kafka` package nor the settings store module is loaded

### Requirement: Every client default that changes delivery semantics is stated

The description SHALL set explicitly, rather than inherit from the client library, every option
whose library default changes what a backend promises:
- **Producers:** acknowledgement by all in-sync replicas; idempotence with one in-flight request per connection; a bounded metadata wait, request timeout and delivery timeout; the request size limit at the message-size ceiling; gzip compression.
- **Group consumers:** the client id the calling backend names; no auto-commit; one record per poll; the offset-reset policy the calling backend names; an explicit session timeout and heartbeat interval; fetch limits at the message-size ceiling; record checksums verified.

A group consumer's client id SHALL be a required argument that every backend building one passes. The broker forms each member id from
it, so a group's membership names the tier instance holding each partition; the library's default
names every member of every tier alike.
- **Partition readers:** no group membership through subscription; no auto-commit; a short request timeout.

The producer's delivery timeout SHALL equal the send wait every backend takes
(`KAFKA_SEND_TIMEOUT_SEC`), and no backend SHALL wait less: a caller that gives up before the producer
does reports as not accepted a record the producer is still retrying, and `IInbox.submit` promises the
opposite — it returns when the message was accepted and raises when it was not, which is what lets its
caller leave a journal row unprocessed and retry.

The composed options SHALL be pinned by tests that read them back from built clients.

Topic auto-creation SHALL be left at the client library's default rather than refused, so whether a
request that names a missing topic creates it is the cluster's configuration. A backend SHALL NOT rely on
either answer: the infrastructure creates the topics (capability `kafka-topic-provisioning`), and on a
cluster that does not create them on demand, a client that names a missing topic fails after its bounded
wait.

#### Scenario: A producer is built with the stated options

- **WHEN** the factory builds a producer
- **THEN** it acknowledges with all in-sync replicas, is idempotent with one in-flight request per connection, bounds its metadata wait, request and delivery timeouts, limits a request to the message-size ceiling and compresses with gzip

#### Scenario: No send waits less than the producer gives it

- **WHEN** the send wait a backend takes is compared with the producer's delivery timeout
- **THEN** the wait is not the shorter of the two

#### Scenario: A group consumer settles nothing on its own

- **WHEN** the factory builds a group consumer for a backend that names an offset-reset policy
- **THEN** auto-commit is off, one record is returned per poll, and the offset-reset policy is the one the backend named

#### Scenario: A group consumer carries the client id its backend names

- **WHEN** the factory builds a group consumer for a backend that names a client id
- **THEN** the consumer's client id is that value

#### Scenario: A send to a missing topic does not create it

- **WHEN** a producer sends to a topic that does not exist, on a cluster that does not create topics on
  demand
- **THEN** the send fails with a timeout after the bounded metadata wait
- **AND** the topic still does not exist

#### Scenario: No client refuses topic creation

- **WHEN** the factory builds a producer, a group consumer and a partition reader
- **THEN** none of them is built with topic auto-creation refused

### Requirement: A Kafka client is built with a pinned protocol version and without network I/O

The broker protocol version SHALL come from `OUROBOROS_KAFKA_API_VERSION`, written as `MAJOR.MINOR`.
It SHALL be refused when it is empty or not `MAJOR.MINOR`. No client SHALL probe the broker for its version.

A producer, a group consumer and a partition reader SHALL each be built without
blocking network I/O, so a tier composes with no broker reachable. A backend SHALL build its clients
lazily, on the first operation that needs them. A group consumer SHALL join its group only when it
is first polled, so a process that holds a consumer it never polls takes no partition.

#### Scenario: Building a client against an unreachable broker returns at once

- **WHEN** each of the three clients is built with the pinned version against an address nothing answers on
- **THEN** each build returns within a fraction of a second
- **AND** none raises a no-brokers-available error

#### Scenario: A missing or unsupported version is refused naming the key

- **WHEN** `OUROBOROS_KAFKA_API_VERSION` is empty or is not `MAJOR.MINOR`
- **THEN** the refusal names `OUROBOROS_KAFKA_API_VERSION`

#### Scenario: Initializing the factory contacts no broker

- **WHEN** the process-wide factory is initialized with a connection and no client is requested
- **THEN** no connection to any broker is attempted

### Requirement: Backends obtain clients only through the client factory interface

Every Kafka backend SHALL receive an `IKafkaClientFactory` as a required keyword argument and SHALL
obtain every client through it. No backend SHALL build a client from the library's client classes
itself. `IKafkaClientFactory` SHALL be an abstract class whose methods are all abstract, with no
default implementation: producer, group consumer, asynchronous group consumer and partition reader.

The asynchronous group consumer SHALL be built from the same connection description as every other
client. One description of the cluster — addresses, security settings, the pinned protocol version,
the timeouts and the topic names — SHALL serve both client libraries, and a second description SHALL
NOT be introduced for the asynchronous one. Which library a client comes from is a property of the
factory, never of the connection.

Adding it SHALL NOT make any existing member optional or defaulted. It is an abstract member
implemented by the real factory and by the in-repo double.

The process-wide `KafkaClientFactory`, `kafka_client_factory`, SHALL be initialized with a connection
by a composition root. Until then, its connection and every method that builds a client SHALL raise
`KafkaClientFactory not initialized: call initialize() first`. A test double SHALL implement the same
interface, so a backend runs unchanged against the double and against a real broker.

#### Scenario: The factory refuses before it is initialized

- **WHEN** the connection, a producer, a group consumer, an asynchronous group consumer or a partition reader is requested from a factory that was never initialized
- **THEN** each request raises naming the missing initialization

#### Scenario: The double and the real factory are interchangeable

- **WHEN** the client behaviour suite runs once over the double's factory and once over a real factory connected to a broker
- **THEN** the same cases pass over both, except the ones the real broker cannot be driven into, which are explicitly declined

#### Scenario: Both libraries read one connection description

- **WHEN** a synchronous client and an asynchronous client are built from one factory
- **THEN** both address the cluster the factory's single connection describes
- **AND** no second connection description exists for the asynchronous library

#### Scenario: The new member is abstract

- **WHEN** the client factory interface is inspected
- **THEN** the asynchronous group consumer is an abstract member
- **AND** every implementation declares it rather than inheriting a default

### Requirement: Each topic has its own producer

The factory SHALL hold at most one producer per full topic name. It builds that producer under a lock
on the first request and returns it to every later request for the topic. A publication to one topic
therefore never waits behind another topic's in-flight request, which an idempotent producer limits
to one per connection.

The factory SHALL NOT flush or close its producers when the process stops, and the client factory
interface SHALL offer no close. Every Kafka backend waits for the broker's acknowledgement before a
publication returns, so at stop a producer holds only records whose publication already failed to its
caller. The client library's exit handler closes a producer with a zero timeout and gives those records
up.

A factory SHALL be initialized before it builds a producer, and a second initialization naming a
different connection SHALL be refused whether or not it has built anything, so no client it hands out
addresses a cluster its holder did not describe. A second initialization naming the connection it
already holds SHALL be accepted, so two roots in one process may each call it.

#### Scenario: One producer per topic

- **WHEN** a producer is requested twice for one topic and once for another, including concurrently
- **THEN** both requests for the first topic return the same producer
- **AND** the other topic's producer is a different one

#### Scenario: The factory offers no close

- **WHEN** the client factory interface is inspected
- **THEN** it has no method that flushes or closes the producers it built

#### Scenario: A factory refuses a second initialization naming another connection

- **WHEN** an initialized factory is initialized again with another connection, whether or not it has built a client
- **THEN** the initialization is refused
- **AND** the factory keeps its connection and its producers

#### Scenario: A factory accepts the connection it already holds

- **WHEN** an initialized factory is initialized again with an equal connection
- **THEN** the call is accepted and the factory keeps that connection

### Requirement: A group consumer's timeouts outlast its poll interval, and every blocking call is bounded

A group consumer's request timeout SHALL exceed both its maximum poll interval and its session
timeout, and its connection idle timeout SHALL exceed its request timeout while staying below the
brokers' own `connections.max.idle.ms`, 600 000 ms by default. The broker holds a join request for up to
the poll interval, a request that times out closes its connection, and the client library refuses a group
consumer whose request timeout does not exceed its session timeout. The maximum poll interval SHALL be an
argument the calling backend passes, and a value below the session timeout SHALL still build. It SHALL
NOT be a library default.

Because that request timeout is also the socket timeout of every request the consumer issues, a poll
interval outside `(0, KAFKA_MAX_POLL_INTERVAL_CEILING_MS]` SHALL be refused rather than composed: above
the ceiling a half-open connection goes unnoticed for the whole interval, and the derived idle timeout
passes the brokers' default so the broker closes first. A backend whose handler may hold the loop longer
than the ceiling SHALL be evicted and its partitions reassigned — which is what a maximum poll interval
means — rather than widen the bound.

Every commit and every position lookup, on any consumer, SHALL pass its own timeout: without one a
synchronous commit retries forever, and a position lookup with no position waits forever during an
outage. A position lookup reports an expired timeout by returning no position, not by raising. Closing a
group consumer SHALL pass its own timeout too, because leaving the group otherwise waits up to the
request timeout. A position a reader needs outside a group SHALL be looked up through a partition reader,
never through a group consumer, whose requests are bounded only by a request timeout longer than the poll
interval.

These bounds SHALL govern an asynchronous group consumer identically. Awaiting a call is not a bound:
a request that never answers suspends its caller for as long as a blocking one would occupy a thread,
and the only difference is that nothing appears to be stuck. Every bound above SHALL therefore be
passed explicitly on the asynchronous client too, and none SHALL be left to that library's defaults,
which differ from the synchronous library's.

Where the asynchronous client requires an explicit start and stop, the backend SHALL bound both and
SHALL stop the consumer on every path that abandons it, so a stopped tier leaves its group rather
than waiting to be evicted.

#### Scenario: A group consumer's request timeout outlasts its poll interval

- **WHEN** a group consumer is built with a maximum poll interval P, including a P below its session timeout
- **THEN** the build succeeds and its request timeout is greater than both P and its session timeout
- **AND** its connection idle timeout is greater than its request timeout and below the brokers' default

#### Scenario: A poll interval above the ceiling is refused

- **WHEN** a group consumer is requested with a maximum poll interval of zero, a negative one, or one above the ceiling
- **THEN** the request is refused naming the argument and the ceiling
- **AND** no client is built

#### Scenario: A position lookup during an outage returns no position within its timeout

- **WHEN** the broker is unreachable and a partition reader with no known position looks its position up with a timeout
- **THEN** the lookup returns no position once that timeout has passed
- **AND** it raises nothing

#### Scenario: The asynchronous consumer carries the same bounds

- **WHEN** an asynchronous group consumer is built
- **THEN** its request timeout, session timeout, poll interval and idle timeout are the ones the
  backend passed, under the same relationships
- **AND** none of them is the library's default

#### Scenario: An asynchronous read against a silent broker returns

- **WHEN** the broker does not answer and the asynchronous consumer is read with a timeout
- **THEN** the read returns within that timeout plus the stated bounds of the requests it makes

#### Scenario: A stopped tier leaves its group

- **WHEN** the tier stops while its asynchronous consumer holds an assignment
- **THEN** the consumer is stopped within its own bound
- **AND** it leaves the group rather than waiting to be evicted

### Requirement: One message-size ceiling binds producers, consumers and topics

The message-size ceiling SHALL be the setting `OUROBOROS_KAFKA_MAX_MESSAGE_BYTES`, an integer of 32 MiB
(33 554 432) by default that SHALL be positive, parsed once into `KafkaConnection.max_message_bytes`. No
module constant SHALL restate it. From the connection it SHALL be:
- the producer's request size limit;
- a consumer's per-partition and per-fetch limits.

A managed cluster may cap its message size below the default, which is why the ceiling is a setting.
It SHALL be one value for a deployment, and every topic's `max.message.bytes`, which the infrastructure
sets, SHALL admit it. A topic whose limit is below the ceiling refuses at the broker a record the
producer accepted, with the library's message-size error, and appends nothing.

Producers SHALL compress with gzip. A record over the ceiling SHALL be refused at the producer with
the library's message-size error, which the client raises before anything is sent; a backend SHALL NOT
repeat that check of its own. It SHALL NOT be truncated, split or dropped silently. The backend
that published it reports the refusal the way its seam prescribes.

#### Scenario: Every client takes the ceiling from the connection

- **WHEN** a connection is built with a non-default `OUROBOROS_KAFKA_MAX_MESSAGE_BYTES` and the factory
  builds a producer, a group consumer and a partition reader
- **THEN** the producer's request size limit and each consumer's fetch limits equal that value

#### Scenario: A ceiling of zero or less is refused

- **WHEN** a connection is built with a message-size ceiling of zero or less
- **THEN** the refusal names `OUROBOROS_KAFKA_MAX_MESSAGE_BYTES`
- **AND** no client is built

#### Scenario: A record under the ceiling is delivered

- **WHEN** a record smaller than the ceiling is sent to a topic
- **THEN** a consumer of that topic receives it whole

#### Scenario: A record over the ceiling is refused at the producer

- **WHEN** a record larger than the ceiling is sent
- **THEN** its send fails with the library's message-size error
- **AND** nothing is appended to the topic

#### Scenario: A topic whose limit is below the ceiling refuses the record at the broker

- **WHEN** a record under the ceiling but over its topic's `max.message.bytes` is sent
- **THEN** its send fails with the library's message-size error
- **AND** nothing is appended to the topic

### Requirement: Kafka client logging carries no credential and cannot flood

The Kafka client library's loggers SHALL never emit a record below INFO, whatever application log
level is configured, including a numeric level below DEBUG. The library logs at DEBUG what must never
reach a log: the admin client's full configuration, credentials included, the key and value of every
record it sends, and every record it fetches. This is a level floor on the library's logger tree, not
a redaction of its messages.

The per-attempt connection failures the library logs through its connection, client, coordinator,
heartbeat, consumer-coordinator and producer-sender loggers, and the plaintext-credentials warning its
PLAIN and SCRAM mechanism loggers repeat on every connection close under `SASL_PLAINTEXT`, SHALL be
emitted at most once per logger, level and message per `BROKER_REPORT_INTERVAL_SEC`. The first record of each interval is always emitted, so a
failure that repeats on every reconnect is still reported, once per interval. The message is part of the
key because the gate exists to hold back one repetition, not a second diagnosis: keyed on the logger and
level alone it would swallow a certificate failure for half a minute because a reconnect line came first.
Records from the library's other loggers SHALL NOT be gated. Configuring logging again SHALL leave exactly one gate on
each gated logger.

#### Scenario: No log level emits the configuration dump

- **WHEN** logging is configured at DEBUG or at a numeric level below it, and the library logs at DEBUG a configuration that contains a SASL password
- **THEN** no record containing the password is emitted

#### Scenario: A client at DEBUG leaks no password

- **WHEN** logging is configured at DEBUG and a client built with a SASL password fails to reach its broker
- **THEN** no emitted record contains the password

#### Scenario: Reconnect failures are reported once per interval

- **WHEN** a gated logger logs the same message at one level many times within one report interval, and again after the interval has passed
- **THEN** one record is emitted for the first interval and one for the next

#### Scenario: A different diagnosis on a gated logger is never held back

- **WHEN** a gated logger logs one message repeatedly and then a different one at the same level within one interval
- **THEN** the repeated message is emitted once and the different one is emitted too

#### Scenario: Other library loggers are not gated

- **WHEN** a Kafka logger outside the gated set logs repeatedly within one interval
- **THEN** every record is emitted

### Requirement: A Kafka reader records the client failures it meets and asks the broker nothing only to observe it

A Kafka reader's `poll` SHALL NOT raise on a client I/O error or on the client's own assertions, such as
a closed consumer or an unassigned partition. It SHALL record them through the shared broker-outage policy
(`ouroboros/contracts/stream_replies.py`, `BrokerOutage`) instead, because a reader loop stops its
transport on any exception out of `poll`. A client I/O error includes one the library lets out from
outside its own error hierarchy, such as a TLS handshake whose certificate does not verify, and the bare
value error the library raises for a topic name the cluster would not admit — which every `send` and
every `subscribe` re-checks, so a `OUROBOROS_KAFKA_TOPIC_PREFIX` the connection deliberately does not
check is met here rather than thrown out of a reader loop. The error classes a reader records SHALL be
declared once, beside the connection description, and a programming error, such as a type or attribute
error, SHALL NOT be among them.

A reader SHALL clear its outage on the first poll that completes without such a failure, whether or not
that poll carried a record and whether or not a settlement was owed. An outage cleared only by traffic
keeps a quiet topic reported as unreachable for as long as it stays quiet, and costs the next, different
outage the traceback that the first report of each outage carries.

A reader SHALL NOT consult the client's bootstrap-connection state, which is false on a healthy cluster,
and SHALL NOT make a request only to learn whether the broker answers. The client library reconnects
internally and returns an empty poll while the broker is down. Such an outage is reported by the
library's own connection logs, which the logging policy gates per interval, and a reader records it only
when a request it makes for its own work fails.

#### Scenario: A TLS failure out of the client is recorded rather than raised

- **WHEN** a reader's client raises a TLS error from a request, because a broker's certificate does not verify
- **THEN** the poll returns nothing and raises nothing, and the reader records an outage
- **AND** a reader loop driving that reader keeps polling, and delivers again once the broker answers

#### Scenario: An outage on a quiet topic clears without a record

- **WHEN** a reader that recorded an outage polls again once the broker answers, and the topic has nothing to deliver
- **THEN** the recovery is reported
- **AND** a later, different failure is reported with its own traceback

#### Scenario: A healthy cluster with no bootstrap connection is not an outage

- **WHEN** a reader's client holds no bootstrap connection but its bounded requests succeed
- **THEN** no outage is recorded

#### Scenario: An idle reader asks the broker nothing beyond its fetch

- **WHEN** a Kafka reader polls a topic with nothing to deliver and nothing to settle
- **THEN** it makes no committed-offset read, no position lookup and no request other than its fetch
- **AND** the poll returns nothing and raises nothing

### Requirement: Kafka backends run in the default gate against an in-repo double

The default test gate SHALL exercise every Kafka backend against an in-repo double of the client
library, in `tests/helpers/kafka_double.py`. The double SHALL:
- implement `IKafkaClientFactory`;
- implement the client calls the Kafka backends use, with the library's signatures and timeout semantics, and no other call;
- raise the library's own error classes;
- place keyed records with the library's own partitioner.

The double SHALL cover the asynchronous group consumer on the same terms: the asynchronous library's
signatures, its own error classes, and its own start and stop. Where the two libraries name the same
behaviour differently, the double SHALL reproduce each library's own naming rather than unify them,
because a backend written against a unified double is a backend that does not compile against either
library.

It SHALL reproduce these library behaviours, each of which a backend can get wrong:
- a subscribed consumer joins its group only when it polls;
- a poll during an outage returns an empty result rather than raising;
- a commit with a timeout raises a timeout error during an outage;
- a position lookup returns no position once its own timeout has passed during an outage;
- a send whose wait expired raises a timeout error, and its record still lands once the outage heals;
- a send to a missing topic waits out the bounded metadata wait and then raises a timeout error, as on a
  cluster that does not create topics on demand;
- a seek or a position lookup on an unassigned partition, a poll on a closed consumer and a send on a closed producer raise an assertion error;
- a partition reader with the earliest reset policy whose position lies below the log start, including a
  log start moved after it was positioned, resumes at the log start;
- a partition reader seeking to the beginning resumes at the oldest record the log still retains;
- a send naming a partition the topic does not have raises an assertion error;
- a record over the size its topic declares fails with the message-size error, and nothing lands;
- a rebalance among live members is eager, and a crashed member's partitions are reassigned;
- a member whose membership a crash dropped is still usable: its next commit is refused as stale, and
  its next poll rejoins the group as a new member and resumes each partition at its committed offset,
  not at the position it had reached;
- a read on an asynchronous consumer that was never started raises rather than returning nothing.

A recording wrapper over the same factory interface, `tests/helpers/kafka_recording.py`, SHALL let a
test observe every client a backend built and every call it made, and make the next call of a named
method raise a given error once. Kafka backend tests SHALL inject failures and observe clients through
it rather than by monkeypatching a client.

One behaviour suite SHALL run against the double in the default gate and against a real broker under
the `integration` marker when `OUROBOROS_TEST_KAFKA_BOOTSTRAP_SERVERS` is set. That run is what proves
the double's fidelity. A behaviour the real broker cannot be driven into SHALL be declined by an
explicit declaration, never by an unconditional skip. The CI configuration SHALL NOT change for this.

The suite SHALL run over the asynchronous consumer as well, so that what the double claims about the
asynchronous library is proven against the library, not assumed from the synchronous one.

A real-broker conformance hook that hands a case a backend whose consumer obtains its work by joining
a group SHALL first wait, within a bound the harness states, until that consumer holds its assignment,
and SHALL fail naming the group when the bound expires. The wait SHALL take nothing from the topic.
It is part of the hook's contract, the state a long-running tier's consumer is already in, and never a
relaxation of a case: a seam promise that applies once a consumer holds its assignment is then tested
as written.

#### Scenario: The double fails the way the library does

- **WHEN** the behaviour suite drives the double through an outage, a position lookup with a timeout during it, a seek on an unassigned partition, a poll after close, a send after close and an oversize send
- **THEN** each raises or returns what the library does, with the library's own error class

#### Scenario: A crashed member rejoins on its next poll

- **WHEN** a group consumer's membership is dropped as a session timeout or a lapsed poll interval would drop it, and that consumer then commits and polls
- **THEN** the commit is refused as stale
- **AND** the poll rejoins the group and is assigned partitions again
- **AND** the first record it then receives is the one at the committed offset, even when it had already received records past it

#### Scenario: The same suite holds against a real broker

- **WHEN** `OUROBOROS_TEST_KAFKA_BOOTSTRAP_SERVERS` points at a broker and the suite runs with `-m integration`
- **THEN** every case the real broker can be driven into passes unchanged
- **AND** every other case is declined by name, reported as skipped with a reason naming the declaration, never as passed

#### Scenario: A real-broker hook hands over a consumer that holds its assignment

- **WHEN** a real-broker conformance hook builds a group-consuming backend for a case on a fresh topic
- **THEN** the backend it returns has a consumer that holds its assignment
- **AND** nothing was taken from the topic while the hook waited
- **AND** a consumer that does not obtain its assignment within the stated bound fails the hook naming
  the group

#### Scenario: The default gate needs no broker

- **WHEN** the default gate runs with no Kafka service and no Kafka environment variable
- **THEN** the behaviour suite runs against the double and the real-broker subclass is not collected as a failure

#### Scenario: The asynchronous consumer is covered by the same suite

- **WHEN** the behaviour suite runs over the asynchronous group consumer against the double, and
  against a real broker under the `integration` marker
- **THEN** the same cases pass over both
- **AND** the double reproduces the asynchronous library's own signatures and error classes rather
  than the synchronous library's

