# task-artifact-publication Specification

## Purpose

How an artifact produced in a task's workdir becomes a stored object: keyed by the producing
task, claimed through a conditional provenance sidecar, and refused when no task is in hand.

## Requirements

### Requirement: A published artifact is keyed by the task that produced it

An artifact a tool publishes from a task's temporary workdir SHALL be stored under a key that carries
that task's id — `uploads/task_artifacts/<task_id>/<path relative to the workdir>` — reusing the
prefix already written for promoted tool results, so that "files this task produced" has one shape.
The key SHALL NOT be the bare file name the model chose, because that name is a deployment-wide
namespace two tasks reach at once.

The key SHALL NOT carry an owner segment. Publication is reachable only from script-skill execution,
which the multi-owner mode refuses at startup, so exactly one owner exists wherever this runs; a
segment that encodes no distinction would read as an authorization boundary that is not there. Owner
isolation of object keys belongs to the storage layer's key construction, applied to every scope at
once.

The publishing call SHALL still take the owner scope as an explicit argument, because the drive it
writes through is resolved for a scope rather than from process configuration.

#### Scenario: Two tasks publish the same file name

- **WHEN** two tasks each publish a file named `report.docx` from their own workdirs
- **THEN** both publications succeed
- **AND** each task's stored object holds its own bytes under its own task-keyed path
- **AND** each task's outbound file send delivers its own bytes

#### Scenario: A file in a workdir subdirectory keeps its relative path

- **WHEN** a script leaves `sub/nested.txt` in its workdir
- **THEN** the published key is that relative path under the task's own prefix

### Requirement: Publication without a task is refused

Publication SHALL be refused when the calling context carries no task id, and the refusal SHALL be
reported to the caller as the tool's existing publication error. The ownership predicate that guards
materialization and outbound sending compares the sidecar's task id against the caller's, and an
empty task id matches nothing — so an artifact published without one is unreachable by every path,
including its producer's.

#### Scenario: A context with no task id publishes nothing

- **WHEN** publication is attempted from a context whose task id is empty
- **THEN** the call is refused
- **AND** no object and no provenance sidecar are written

### Requirement: Creation is conditional, and the provenance sidecar is what is claimed

Publication SHALL claim each artifact by creating its provenance sidecar with the storage layer's
conditional create, and SHALL write the bytes only after winning that claim. It SHALL NOT decide on a
prior existence probe followed by an unconditional write, because the two writers that can still meet
both pass such a probe and then settle their sidecars in the opposite order from their bytes.

A claim lost to a sidecar naming another task SHALL be a refusal. A claim lost to a sidecar naming the
calling task SHALL proceed, so that a task re-publishing its own artifact overwrites its own file.

Every refusal a caller can trigger — a reserved provenance file name, an over-size file — SHALL still
be raised before anything is written, so that one refused file publishes none.

#### Scenario: The loser of a claim writes nothing

- **WHEN** two publications race for one key and one wins the sidecar claim
- **THEN** the loser is refused
- **AND** the stored bytes and the stored sidecar name the same task

#### Scenario: A task re-publishing its own artifact succeeds

- **WHEN** a task publishes a file whose key already holds a sidecar naming that same task
- **THEN** the publication succeeds and the object holds the new bytes

#### Scenario: An over-size file refuses the whole publication

- **WHEN** one file in a publication exceeds the outbound size limit
- **THEN** the call is refused
- **AND** none of the files in that call is written

### Requirement: Both key shapes resolve for one release

Materialization SHALL resolve a drive path handed to a model by an earlier release. A path naming an
object that exists SHALL resolve to that object; a bare `uploads/<name>` that does not resolve SHALL
be retried under the calling task's artifact prefix. The literal path SHALL be tried first, so an
object published flat by an earlier release is neither shadowed nor moved.

This dual resolution SHALL live in the materialization path alone, so the outbound file tool inherits
it rather than restating it. It SHALL be removed once no live transcript can carry a flat path, and
its temporary nature SHALL be recorded where it is written.

#### Scenario: A flat path from an earlier release still resolves

- **WHEN** a task references an object published flat before this change, whose sidecar names that
  task
- **THEN** it materializes from the flat key

#### Scenario: A bare name resolves under the task's prefix

- **WHEN** a task references `uploads/report.docx` and no such object exists, while
  `uploads/task_artifacts/<its task id>/report.docx` does
- **THEN** it materializes from the task-keyed object

#### Scenario: Another task's flat object is still refused

- **WHEN** a task references a flat path whose sidecar names a different task
- **THEN** materialization refuses it with the ownership refusal, and no fallback overrides that
