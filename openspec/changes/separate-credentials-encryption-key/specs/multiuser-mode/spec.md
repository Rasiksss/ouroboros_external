## MODIFIED Requirements

### Requirement: No egress authorization kind is refused under the mode

The mode's declared refusal set SHALL NOT refuse an MCP entry or an A2A remote-agent entry for the
authorization kind it declares. The security context a `user_headers` call reads and the personal token
a `user_token` call reads are all stored in the runtime database every process of the mode shares —
personal MCP tokens in `mcp_user_tokens`, personal A2A tokens in `a2a_user_tokens` — so the agent tier
that makes a call reads what the browser transport tier or a writer stored, whichever replica makes the
call; `none` and `mtls` add nothing to a call. Every tier SHALL seal and open those rows under the
`OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` the deployment gives them all. An A2A task handle is held by no
process and no store: it is computed from that same key, so a handle one agent-tier replica reported
opens on every other, and a task started through one replica is followed through another. An entry of any
kind SHALL remain subject to the mode's other refusals, such as the instance-local address refusal and the
refusal of a `user_headers` name the header collector does not declare, for MCP and A2A alike.

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

- **WHEN** two agent-tier processes compose over the same `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, and a
  task started through one of them by owner `o` and agent `a` with remote agent `r` reported a handle
- **THEN** `a2a_task_status` with that handle, called by a run of `o` and `a` in the other process, is
  answered for that task
- **AND** opening the handle reads no store
