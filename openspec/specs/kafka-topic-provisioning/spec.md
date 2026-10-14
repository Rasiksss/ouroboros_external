# kafka-topic-provisioning Specification

## Purpose

The deployment step, declarations, and verification that create and check the Kafka topics the broker
seams use, settle the start positions the declared consumer groups need, and reclaim disk below every
group's settled floor. Topics are created by `ouroboros kafka-topics` before any tier starts, never by
the broker or by a tier on first use; each topic declares its partitioning, replication, retention, and
durability; present-start groups have their positions fixed once by provisioning; verification is bounded
and read-only; and unread records on backlog-start topics are never evicted, with disk reclaimed only
below the settled floor.

## Requirements

### Requirement: Kafka topics belong to the infrastructure, and no code administers them

The Kafka topics the broker seams use, `{prefix}.inbound`, `{prefix}.outbound`, `{prefix}.agent-events`,
`{prefix}.memory-events` and `{prefix}.task-stops` under `OUROBOROS_KAFKA_TOPIC_PREFIX`, SHALL be created by the infrastructure that
runs the cluster, with the partition counts, replication, retention and message-size limits it sets. No
production code SHALL create or delete a topic, describe or alter its configuration, compare it with a
declaration, add partitions to it, delete its records, or commit a consumer group's position from outside
a member of that group. No production module SHALL build a Kafka admin client, and the client factory
interface SHALL offer none.

A tier SHALL neither create nor check a topic when it starts. A topic that is missing when a seam first
uses it is met the way the seams meet a broker that does not answer: a publish fails after its bounded
wait, and a reader waits.

The deployment documentation SHALL state what the infrastructure provides: the five topic names, that
each topic's `max.message.bytes` admits `OUROBOROS_KAFKA_MAX_MESSAGE_BYTES`, that inbound, outbound and
task-stops are written and read on partition 0 only, the rights each tier's principal needs, and that
retention is the infrastructure's, so a record left unread past it is lost. The docker stand's broker SHALL create the
topics on first use.

#### Scenario: No production code administers a topic

- **WHEN** the production packages are inspected
- **THEN** none of them imports the Kafka admin client or names an admin operation: creating or deleting a
  topic, describing or altering configs, adding partitions, deleting records, or listing a group's offsets
- **AND** the client factory interface has no method that builds an admin client

#### Scenario: The stand's broker creates the topics the fleet names

- **WHEN** the docker deployment description is read with its `kafka` profile
- **THEN** its broker enables topic auto-creation and admits the fleet's message-size ceiling
- **AND** it declares no job that creates or checks topics
