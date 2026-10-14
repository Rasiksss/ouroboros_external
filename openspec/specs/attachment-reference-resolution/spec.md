# attachment-reference-resolution Specification

## Purpose

How a stored object is reached: only through a reference that names it, and only by a run that
was given the attachment. Covers the file-download route's resolution rules and the criterion
every attachment-reading tool applies.

## Requirements

### Requirement: A stored object is reachable only through a reference that names it

The file-download route SHALL resolve a request to a stored object only through a reference that
names that object — an attachment id, an id-and-filename pair, a canonical `chat/` or `generated/`
storage key, an exact storage key, or an explicit alias record. It SHALL NOT resolve a request by
comparing the requested filename against the basenames of stored keys, and SHALL NOT enumerate a
storage prefix in order to answer a download.

A filename is chosen by the person who uploaded the file and is guessable; an attachment id is not.
Resolving by filename converts every unguessable reference in the deployment into a guessable one,
and the object it finds carries no metadata under the key that was matched, so expiry and status
checks are skipped along the way.

#### Scenario: A bare filename does not resolve to a stored attachment

- **WHEN** exactly one attachment is stored under `chat/<attachment_id>/report.md` and a download is
  requested for `report.md`
- **THEN** the response reports the file as not found
- **AND** the stored bytes do not appear in the response

#### Scenario: A reference that names the object still resolves

- **WHEN** a download is requested for `<attachment_id>/report.md`, for the canonical key
  `chat/<attachment_id>/report.md`, or for the bare `<attachment_id>`
- **THEN** the stored bytes are returned

#### Scenario: An unresolvable request enumerates nothing

- **WHEN** a download request matches no reference
- **THEN** the route reports the file as not found
- **AND** no storage prefix is listed while answering it

### Requirement: A tool reaches only an attachment of the chat it runs in

A tool that materializes a stored chat attachment — to deliver it outbound, to read its text, or to
analyse it as an image — SHALL require the record to be owned by the running scope and to name the
chat the run is in, and SHALL refuse otherwise. An id among the attachments bound to the current run
SHALL be accepted as well, whatever chat its record names, so the criterion is never narrower than
the run's own attachments and a run that carries an attachment across chats keeps working.

The criterion SHALL be one predicate applied by every tool that reaches an attachment by id, not a
check restated in each of them.

The chat is the axis because the owner pair is not one. A record carries `owner_id`, `agent_id`,
`surface_id` and `chat_id`, but the Dialogs transport mints one configured `owner_id` for every chat
on the instance: the pair separates deployments, not conversations, and a single chat may carry
several people. The run's bound attachments are no longer the only evidence of ownership at this
layer — that was true before the record carried any owner, and it is not true now — but the owner
pair alone would place every conversation on the instance in reach of every other.

A refusal SHALL be indistinguishable from the answer for an id that was never stored. A
distinguishable refusal would confirm that an id the run was shown, but not given, is live in another
chat under the same configured owner.

#### Scenario: A bound id is delivered

- **WHEN** a run whose bound attachments include the id asks to send it
- **THEN** the attachment is materialized and accepted for delivery

#### Scenario: An attachment of another chat is not reachable

- **WHEN** a run in one chat asks to send, read, or analyse a stored attachment whose record names a
  different chat, and the id is not bound to the run
- **THEN** the tool refuses
- **AND** the object is not read and nothing is handed to the outbound boundary

#### Scenario: An earlier attachment of the same chat is reachable

- **WHEN** a run asks for a stored attachment that was uploaded in an earlier message of the same
  chat, so that it is not among the attachments bound to this run
- **THEN** the attachment is materialized and the tool succeeds

#### Scenario: A bound id is reachable whatever chat it names

- **WHEN** a run whose bound attachments include the id asks for it, and the record names a different
  chat
- **THEN** the attachment is materialized and the tool succeeds

#### Scenario: The refusal is the not-found answer

- **WHEN** a tool refuses an id whose record exists under the same owner but names another chat
- **THEN** the result is the one an id that was never stored produces
- **AND** neither the filename nor any other field of that record is disclosed

#### Scenario: Every tool that reaches an attachment by id applies the same criterion

- **WHEN** each tool that sends, reads, or analyses a stored attachment by id is called with an id
  that is neither bound to the run nor a record of the run's chat
- **THEN** each of them refuses
- **AND** none of them reads the object

#### Scenario: A refused reach is recorded

- **WHEN** a tool refuses an id under this criterion
- **THEN** the refusal is logged with the run's task id and the reason
