## MODIFIED Requirements

### Requirement: A remote task is followed only by the owner and agent that started it

Every `task_id` an A2A result reports SHALL be a task handle, `<remote task id>~<mac>`, where `mac` is the
first 32 lowercase hexadecimal characters of HMAC-SHA256 over the compact JSON array
`["a2a-task", <remote agent id>, <remote task id>, <owner id>, <agent id>]` under a key derived, with the domain
label `ouroboros:a2a-task-handle:v1`, from `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` as HMAC-SHA256 of the label
under that key. `OUROBOROS_CHAT_ENCRYPTION_KEY` SHALL play no part in it. A handle SHALL be bound to the
owner, the agent and the remote agent, and SHALL NOT be bound to the chat or the surface: a task started in
one chat SHALL be followed from any chat or surface of the same owner and agent.

`a2a_task_status`, `a2a_task_cancel` and a call of an agent tool with `task_id` SHALL open the handle for the
calling run's owner and agent and the addressed remote agent first — before either store is read, before
credentials are resolved and before any request is sent. A value that does not open — a handle sealed for
another owner, agent or remote agent, a bare remote task id, or a handle whose `mac` was altered — SHALL be
refused with `⚠️ A2A_ERROR` naming the agent and the value as given, stating that it was not started by this
owner and agent with that agent, and saying to pass `task_id` exactly as the result that started the task
reported it. A request that names a task SHALL name it by its remote task id; the remote agent SHALL never
receive a handle.

The key SHALL be validated when the task handles are composed, so that a key that is not exactly 64 bytes of
base64, whitespace removed, refuses the start rather than a call. A task started before handles existed SHALL
NOT be followable, and a changed key SHALL invalidate every handle sealed under the previous one. Moving the
handles from the chat encryption key to the credentials key is such a change: a handle reported before it
SHALL NOT open after it.

#### Scenario: Every result naming a task names its handle

- **WHEN** results report a remote task as submitted, running, completed with no output, ended in a failed
  state, `input-required`, `auth-required`, timed out, cancelled, or unanswered on a continuation
- **THEN** each names the task by the remote task id followed by `~` and 32 lowercase hexadecimal characters
- **AND** a call answered with a message rather than a task reports no handle

#### Scenario: Another owner's handle is refused before any store read or request

- **WHEN** a run of owner `o2` and agent `a` passes a handle sealed for owner `o1` and agent `a` to
  `a2a_task_status`, to `a2a_task_cancel`, or as the `task_id` of the agent tool
- **THEN** the result is `⚠️ A2A_ERROR` naming the handle as given and stating that it was not started by this
  owner and agent with that agent
- **AND** neither store is read
- **AND** no request reaches the remote agent

#### Scenario: A handle of another agent or another remote agent is refused

- **WHEN** a run of owner `o` and agent `a2` passes a handle sealed for owner `o` and agent `a1`, or passes a
  handle sealed for remote agent `r1` with agent id `r2`
- **THEN** the call is refused and no request is sent

#### Scenario: A bare or forged task id is refused

- **WHEN** a run passes a remote task id without a `mac`, or its own handle with one character of the `mac`
  changed
- **THEN** the call is refused and no request is sent

#### Scenario: A handle opens from another chat and surface of the same owner and agent

- **WHEN** a browser run of owner `o` and agent `a` in chat `c1` starts a task, and a dialogs run of `o` and
  `a` in chat `c2` reads it with the reported handle
- **THEN** the task read is sent for the remote task id

#### Scenario: The remote agent never receives a handle

- **WHEN** a run continues, reads and cancels a task by its handle
- **THEN** the send, the task reads and the cancel name the remote task id
- **AND** no request body or header carries the handle's `mac`

#### Scenario: A changed key invalidates open handles

- **WHEN** a handle sealed under one key is opened by task handles composed with another key
- **THEN** it does not open

#### Scenario: A handle is keyed from the credentials key

- **WHEN** task handles composed with an `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` seal a handle
- **THEN** its `mac` is the first 32 hexadecimal characters of HMAC-SHA256 over the handle's JSON array under
  HMAC-SHA256 of `ouroboros:a2a-task-handle:v1` keyed with the 64 bytes of that setting

#### Scenario: An invalid key refuses the start

- **WHEN** task handles are composed with an `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` that is not 64 bytes of
  base64, a valid 32-byte chat encryption key among them
- **THEN** composition fails naming `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`
- **AND** the failure renders no part of the key
