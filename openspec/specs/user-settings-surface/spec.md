# user-settings-surface Specification

## Purpose

The HTTP surface over the keys an owner may override, one agent at a time. It reports what is
in force for an agent and where each value came from, stores and clears overrides, and reaches no
configuration key outside the declaration the reader and the writer share. The owner arrives in the
same header the registry surface reads, resolved by the same resolver; the agent is named by the
request path and verified against the registry, while isolation rests on every statement being keyed
on the owner together with the agent rather than on that verification. It is one router, included by
the single-process runtime and by the tier that serves it in the multi-user mode.
## Requirements
### Requirement: An owner reads the settings in force for one agent over HTTP

The system SHALL offer an HTTP surface that reports, for one of the owner's agents and for each key
in the declaration, the value in force and whether that value came from a stored override or from
the instance configuration.

The surface SHALL reach no configuration key outside the declaration. The set of keys it reports
SHALL equal the declaration exactly, so that widening the surface is impossible without changing the
declaration that the reader and the writer also obey.

#### Scenario: The surface reports what is in force

- **WHEN** an owner reads an agent's settings and one key has a stored override
- **THEN** the response carries the overridden value for that key marked as an override
- **AND** the instance value for every other key marked as coming from the instance

#### Scenario: The surface is bounded by the declaration

- **WHEN** the keys the surface reports are compared with the declaration
- **THEN** the two sets are equal

#### Scenario: A key outside the declaration is unreachable

- **WHEN** a request names a configuration key outside the declaration
- **THEN** it is refused as malformed
- **AND** nothing is stored or reported for it

### Requirement: An owner sets and clears the overrides of one agent

The system SHALL allow an owner to store an override for one key of one of their agents and to clear
it. Clearing an override SHALL remove the stored row, so that the key resolves to the instance value
again; clearing a key that has no override SHALL be reported as an absence rather than as a failure
of the request.

A stored value SHALL be rejected when it does not parse as the key's type, rather than stored and
silently ignored at read time.

Storing and clearing SHALL each invalidate the cached entry for that owner and agent
(`user-settings-cache`) before the response is returned, including when clearing found no row: the
surface cannot tell whether a reader has cached the scope, and a request answered before the
invalidation lands would report a value the readers do not yet resolve. The write to the store is
what makes the change durable and the invalidation is what makes it visible; a write whose
invalidation fails SHALL fail the request rather than report success over a cache still holding the
old value.

#### Scenario: An override is stored and takes effect

- **WHEN** an owner stores an override for a key of one agent
- **THEN** reading that agent's settings in force reports the stored value
- **AND** a reader resolving that key under the matching scope resolves the stored value

#### Scenario: A cleared override returns the instance value

- **WHEN** an owner clears a stored override
- **THEN** reading the settings in force reports the instance value for that key

#### Scenario: Clearing what is not there is an absence

- **WHEN** an owner clears a key that has no stored override
- **THEN** the response states the absence
- **AND** no other key is affected

#### Scenario: A value that does not parse is refused

- **WHEN** a request stores a value that does not parse as the key's type
- **THEN** it is refused
- **AND** nothing is stored

#### Scenario: A stored override takes effect over a cached scope

- **WHEN** a reader has cached an agent's scope and an owner then stores an override for it
- **THEN** the reader resolves the stored value

#### Scenario: Clearing an absent override still invalidates

- **WHEN** an owner clears a key that has no stored override and the scope is cached
- **THEN** the cached entry is dropped

#### Scenario: A failed invalidation is not reported as success

- **WHEN** the row is written but the cached entry cannot be dropped
- **THEN** the request fails

### Requirement: The owner settings surface writes MCP server ids as text

The surface SHALL include `OUROBOROS_USER_ENABLED_MCP_IDS` in its bounded key declaration and SHALL accept its
value through the existing string-valued PUT body. Reading the settings in force SHALL return the
parsed ids as a JSON array. Clearing the row SHALL return the value to the instance env default
(an empty list by default).

#### Scenario: A string value round-trips as ids

- **WHEN** an owner stores `["search", "calendar"]` for one agent
- **THEN** reading that agent's settings returns `OUROBOROS_USER_ENABLED_MCP_IDS` as `["search", "calendar"]`

#### Scenario: Clearing removes MCP access

- **WHEN** an owner clears `OUROBOROS_USER_ENABLED_MCP_IDS`
- **THEN** reading that agent's settings returns the instance env default (an empty list by default)

### Requirement: The owner arrives authenticated; the agent is named by the request path

The owner SHALL be taken from the same request header the registry surface reads, by the same
resolver: the header when present; the instance's configured owner while the deployment is not
declared multi-user; a refusal when it is. The surface SHALL name no transport.

The agent SHALL be named by the request path and never by a header. The owner is what a fronting
proxy authenticated; the agent is a resource the caller chose, and the two SHALL NOT occupy the same
kind of slot. Every operation of the surface names an agent.

#### Scenario: A request with no owner is refused in the multi-user mode

- **WHEN** a request carries no owner header and the deployment is declared multi-user
- **THEN** it is refused as forbidden

#### Scenario: The agent is never read from a header

- **WHEN** a request carries a header naming an agent
- **THEN** it has no effect on which agent the request addresses

### Requirement: A request naming an agent the owner does not have is refused

The system SHALL verify, against the agent registry, that the agent a request names belongs to the
requesting owner, and SHALL refuse the request as a missing resource when it does not. The surface
mints no agent: an owner's default agent exists once that owner has arrived through a transport.

Isolation SHALL NOT depend on that verification: every statement SHALL be keyed on the owner together
with the agent, so a request naming an agent of another owner reaches only rows under the requesting
owner's own id. The verification exists to keep rows for agents that do not exist out of the store.

#### Scenario: An agent of another owner is not found

- **WHEN** a request names an agent belonging to a different owner
- **THEN** it is refused as a missing resource
- **AND** nothing is stored

#### Scenario: Isolation does not rest on the check

- **WHEN** the statements the surface issues are inspected
- **THEN** every one of them is keyed on the owner together with the agent

### Requirement: The surface is one router, included by both composition roots

The surface SHALL be a router whose module holds its settings writer and its agent registry, over
the shared engine holder, so a root has nothing to initialise on it. Both the single-process runtime
and the tier that serves it in the multi-user mode SHALL include that one router on an application
they build, so the capability is available in both deployment modes and its operations are the same.

It SHALL sit beside the shared browser route collection rather than inside it, so the transport tier
does not serve a surface another tier owns.

#### Scenario: Both modes offer the same operations

- **WHEN** the surface served by the single-process runtime is compared with the one the tier serves
- **THEN** the two offer the same operations, built from the same definitions

#### Scenario: The transport tier does not serve it

- **WHEN** the route set the transport tier mounts is inspected
- **THEN** it contains no route of this surface

### Requirement: An owner lists the MCP servers of one agent

The surface SHALL list, for one of the owner's agents, every enabled MCP server of the deployment's
list, in the order the list declares them, and SHALL list none while the MCP kill switch is off — the
same gate the agent tier connects behind. Each server SHALL carry its id, name, transport and
authentication kind; whether the agent's resolved `OUROBOROS_USER_ENABLED_MCP_IDS` selects it; whether it takes a personal
token; whether this owner may store one for it; whether the deployment gives it a static token; and the
expiry of this owner and agent's unexpired personal token for it, or nothing.

The listing SHALL be read-only: it changes no selection and stores nothing. It SHALL NOT carry a server's
address, the static token or a personal token.

A server SHALL take a personal token exactly when its authentication kind is `user_token` and its transport
is `streamable_http`, by the same rule the runtime composition uses; over `sse` a personal token is never
sent.

#### Scenario: The listing reports what an owner may do with each server

- **WHEN** the deployment lists an enabled `user_token` server over `streamable_http`, a `user_token`
  server over `sse` and a `none` server, and the owner holds an unexpired token for the first
- **THEN** all three are listed in declaration order
- **AND** only the first takes a personal token and carries that token's expiry

#### Scenario: A disabled server and a switched-off deployment list nothing

- **WHEN** a server is disabled, or the MCP kill switch is off
- **THEN** that server, or every server, is absent from the listing

#### Scenario: An expired token is not reported

- **WHEN** the owner's stored token for a server has expired
- **THEN** the listing reports no expiry for that server

#### Scenario: The listing carries no address and no token

- **WHEN** the listing is returned for a server with a static token and a stored personal token
- **THEN** the response contains neither token and not the server's address

### Requirement: An owner lists the A2A agents of one agent

The surface SHALL list, at `GET /{agent_id}/a2a-agents` under `/api/owner-settings`, for one of the owner's
agents, every connectable A2A agent of the deployment's list — the enabled entries of
`security_config/a2a.py` with a non-empty id, the first entry of each id, by `connectable_a2a_agents`, the
one rule the A2A manager connects by — in the order the list declares them, under `agents`, and SHALL list
none while `A2A_EGRESS_ENABLED` is off — the same gate the agent tier connects behind. Each agent SHALL
carry its `id`, `name` and authentication kind (`auth_kind`); whether it takes a personal token
(`personal_token_supported`); whether this owner may store one for it (`personal_token_writable`); whether
the deployment gives it a static token (`has_static_token`); and the expiry of this owner and agent's
unexpired personal A2A token for it, or nothing (`personal_token_expires_at`).

The listing SHALL be read-only: it selects no agent and stores nothing, and it adds no key to the owner
setting declaration. It SHALL NOT carry an agent's address, the static token or a personal token. It SHALL
follow the configured list, not whether a tier's connection to the agent succeeded.

An A2A agent SHALL take a personal token exactly when its authentication kind is `user_token`, by the same
rule the runtime composition uses (`takes_personal_token`). A personal A2A token and a personal MCP token
SHALL be separate records even under one id: this listing SHALL report only the expiry of an A2A token, and
the MCP server listing only that of an MCP token.

#### Scenario: The listing reports what an owner may do with each agent

- **WHEN** the deployment lists an enabled `user_token` agent `research` with a static token, a
  `user_headers` agent and a `none` agent, and the owner holds an unexpired A2A token for `research`
- **THEN** all three are listed in declaration order
- **AND** only `research` takes a personal token, reports a static token and carries that token's expiry

#### Scenario: A disabled agent and a switched-off deployment list nothing

- **WHEN** an agent is disabled, or `A2A_EGRESS_ENABLED` is off
- **THEN** that agent, or every agent, is absent from the listing

#### Scenario: An id declared twice is listed once

- **WHEN** the deployment's list declares two enabled entries with the id `research`
- **THEN** the listing carries `research` once, with the name and authentication kind of the first entry

#### Scenario: An expired token is not reported

- **WHEN** the owner's stored A2A token for an agent has expired
- **THEN** the listing reports no expiry for that agent

#### Scenario: The listing carries no address and no token

- **WHEN** the listing is returned for an agent with a static token and a stored personal token
- **THEN** the response contains neither token and not the agent's address

#### Scenario: A token of the other kind with the same id is not reported

- **WHEN** the owner holds an unexpired MCP token for server `hr` and no A2A token, and the A2A list declares
  a `user_token` agent `hr`
- **THEN** the A2A listing reports no expiry for `hr`
- **AND** when the owner then holds an A2A token for `hr` and clears the MCP one, the MCP server listing
  reports no expiry for server `hr`

### Requirement: An owner sets and clears a personal MCP token with a bounded TTL

The surface SHALL let an owner store a personal token for one server and one of their agents, with a TTL
in seconds the request chooses, and SHALL store it sealed, keyed by the server, the owner and the agent,
until now plus that TTL, replacing any token already stored for them. The response SHALL carry the server
id and the expiry. A TTL below 60 seconds or above 90 days SHALL be refused as unprocessable. A token that
is empty, longer than 8192 characters, or not a value a request header can hold SHALL be refused as a bad
request, and nothing SHALL be stored.

The surface SHALL let an owner clear that token, reporting whether one was stored, whatever the MCP kill
switch and the server list say now, since a revocation cannot widen who holds a token and a token left
behind would be sent again once its server returns. A write naming a server id outside the enabled list
SHALL be refused as a missing resource, and a write for a server that takes no personal token SHALL be
refused as a bad request.

Every statement SHALL be keyed on the server, the owner and the agent, so a write or a clear names only the
requesting owner's own row. An unusable encryption key SHALL refuse the request as unavailable, naming the
setting and not the key.

#### Scenario: A stored token is the one the runtime loads

- **WHEN** an owner stores a token for a streamable `user_token` server and one agent with a TTL of one hour
- **THEN** the response's expiry is one hour from now
- **AND** the runtime's token store loads that token for that server, owner and agent

#### Scenario: A TTL outside the bounds is refused

- **WHEN** a request names a TTL of 59 seconds, or of 90 days and one second
- **THEN** it is refused as unprocessable
- **AND** nothing is stored

#### Scenario: A clear removes the token

- **WHEN** an owner clears a stored token and then clears it again
- **THEN** the first clear reports that a token was cleared and the second that none was
- **AND** the runtime's token store loads nothing for that server, owner and agent

#### Scenario: A server that takes no personal token refuses the write

- **WHEN** an owner writes a token for a `none` server or a `user_token` server over `sse`
- **THEN** the request is refused as a bad request

#### Scenario: A token is revoked while its server is not offered one

- **WHEN** an owner holds a stored token for server `hr` and clears it while the MCP kill switch is off or
  `hr` is disabled
- **THEN** the clear reports that a token was cleared
- **AND** the runtime's token store loads nothing for that server, owner and agent

#### Scenario: Another owner's token is untouched

- **WHEN** one owner clears a server's token for an agent id that another owner also holds a token under
- **THEN** the other owner's token still loads

### Requirement: An owner sets and clears a personal A2A token with a bounded TTL

The surface SHALL let an owner store, at `PUT /{agent_id}/a2a-agents/{remote_agent_id}/token` under
`/api/owner-settings` with the body `{token, ttl_sec}` and no other field, a personal token for one A2A agent
and one of their agents, with a TTL in seconds the request chooses, and SHALL store it sealed, keyed by the
A2A agent, the owner and the agent, until now plus that TTL, replacing any A2A token already stored for
them. The response SHALL carry `remote_agent_id` and `expires_at`. A TTL below 60 seconds or above 90 days
SHALL be refused as unprocessable. A token that is empty, longer than 8192 characters, or not a value a
request header can hold SHALL be refused as a bad request, and nothing SHALL be stored. These bounds and
the body parsing SHALL be the ones the personal MCP token route applies.

The surface SHALL let an owner clear that token at `DELETE` on the same path, answering `remote_agent_id`
and `cleared`, reporting whether one was stored, whatever `A2A_EGRESS_ENABLED`, the A2A list and the
agent's authentication kind say now, since a revocation cannot widen who holds a token and a token left
behind would be sent again once its agent returns. A write naming an A2A agent id outside the connectable
list SHALL be refused as a missing resource, and a write for an agent that takes no personal token SHALL be
refused as a bad request.

Every statement SHALL be keyed on the A2A agent, the owner and the agent, in the store of A2A tokens, so a
write or a clear names only the requesting owner's own A2A row and never reaches an MCP token stored under
the same id. An unusable encryption key SHALL refuse the request as unavailable, naming the setting and not
the key. The personal MCP token routes and their payloads SHALL NOT change.

#### Scenario: A stored A2A token is the one the runtime loads

- **WHEN** an owner stores a token for a `user_token` A2A agent `research` and one agent with a TTL of one
  hour
- **THEN** the response carries `remote_agent_id` `research` and an expiry one hour from now
- **AND** the runtime's token store loads that token for the A2A agent target `research`, that owner and
  that agent
- **AND** it loads nothing for the MCP server target `research`

#### Scenario: A TTL outside the bounds is refused

- **WHEN** a request names a TTL of 59 seconds, or of 90 days and one second
- **THEN** it is refused as unprocessable
- **AND** nothing is stored

#### Scenario: A token a header cannot hold is refused

- **WHEN** a request stores an empty token or one of 8193 characters for a `user_token` A2A agent
- **THEN** it is refused as a bad request
- **AND** nothing is stored

#### Scenario: A clear removes the A2A token

- **WHEN** an owner clears a stored A2A token and then clears it again
- **THEN** the first clear reports that a token was cleared and the second that none was
- **AND** the runtime's token store loads nothing for that A2A agent, owner and agent

#### Scenario: An agent that takes no personal token refuses the write

- **WHEN** an owner writes a token for a `none`, `user_headers` or `mtls` A2A agent
- **THEN** the request is refused as a bad request
- **AND** nothing is stored

#### Scenario: An unknown or disabled agent is not found

- **WHEN** an owner writes a token for an A2A agent id the list does not declare, for a disabled entry, or
  while `A2A_EGRESS_ENABLED` is off
- **THEN** the request is refused as a missing resource
- **AND** nothing is stored

#### Scenario: A token is revoked while its agent is not offered one

- **WHEN** an owner holds a stored A2A token for agent `research` and clears it while `A2A_EGRESS_ENABLED` is
  off, `research` is disabled, or `research` no longer takes a personal token
- **THEN** the clear reports that a token was cleared
- **AND** the runtime's token store loads nothing for that A2A agent, owner and agent

#### Scenario: Another owner's A2A token is untouched

- **WHEN** one owner clears an A2A agent's token for an agent id that another owner also holds a token under
- **THEN** the other owner's token still loads

#### Scenario: An MCP token with the same id is untouched

- **WHEN** an owner holds a personal MCP token for server `hr` under one agent, then stores and clears a
  personal A2A token for agent `hr` under the same agent
- **THEN** the runtime's token store still loads the MCP token for server `hr`, that owner and that agent

### Requirement: The configured owner stores no personal token

The surface SHALL refuse, as forbidden, a request that stores a personal token — for an MCP server or for an
A2A agent — when the requesting owner is the instance's configured owner, because dialogs and the skill
bridge run every peer as that owner and each would send the token. It SHALL allow that owner to clear one of
either kind, since a revocation cannot widen who holds a token. The MCP server listing and the A2A agent
listing SHALL each report, for that owner, that no entry's personal token may be stored.

#### Scenario: The configured owner's write is forbidden

- **WHEN** the configured owner stores a token for a server that takes one
- **THEN** it is refused as forbidden
- **AND** nothing is stored

#### Scenario: The configured owner's A2A write is forbidden

- **WHEN** the configured owner stores a token for a `user_token` A2A agent
- **THEN** it is refused as forbidden
- **AND** nothing is stored

#### Scenario: The configured owner may clear

- **WHEN** a token for an MCP server or for an A2A agent is stored under the configured owner and that owner
  clears it
- **THEN** the clear reports that a token was cleared

#### Scenario: The listing tells the configured owner it may not write

- **WHEN** the configured owner lists an agent's MCP servers or its A2A agents
- **THEN** every server and every A2A agent reports that no personal token may be stored

### Requirement: A personal token is never returned or echoed

No response of the surface SHALL carry a personal token, MCP or A2A, including every refusal and every
validation error, and the surface SHALL neither log nor cache one. A malformed write body on either token
route — a missing field, an unknown field, a value of the wrong type, or a body that is not an object —
SHALL be refused as unprocessable without rendering any submitted value.

#### Scenario: A refused token is not echoed

- **WHEN** a request stores a token containing a line break, on the MCP server token route or the A2A agent
  token route
- **THEN** it is refused as a bad request
- **AND** the response does not contain any part of the token

#### Scenario: A malformed body does not echo the token

- **WHEN** a write body on either token route carries the token but omits the TTL, or adds an unknown field
- **THEN** it is refused as unprocessable
- **AND** the response does not contain the token

### Requirement: The owner settings surface writes canonical enabled skill ids as text

The surface SHALL include `OUROBOROS_USER_ENABLED_SKILL_IDS` in its bounded key declaration and SHALL
accept its value through the existing string-valued PUT body. Reading the settings in force SHALL
return the parsed values as a JSON array. Clearing the row SHALL restore the instance `AppSettings`
value for the next task. An explicit empty value SHALL remain an empty allowlist rather than restore
the instance value.

The surface SHALL name the agent in the route and resolve the owner through the existing authenticated
owner boundary. Storage and cache invalidation SHALL use that complete owner-and-agent scope and this
exact key. The route SHALL NOT accept skill package locations, source configuration, payloads, or
credentials.

#### Scenario: A JSON string round-trips as canonical enabled ids

- **WHEN** an owner stores `["distribution:calendar-orientation", "distribution:tasks-scheduler"]`
  for one agent
- **THEN** reading that agent's settings returns those values in `OUROBOROS_USER_ENABLED_SKILL_IDS`

#### Scenario: A comma-separated string is normalized

- **WHEN** an owner stores the same non-empty skill id more than once in comma-separated text
- **THEN** reading the setting returns that trimmed id once, preserving first occurrence order

#### Scenario: An empty override enables nothing

- **WHEN** the instance value enables skills and an owner stores `[]` for one agent
- **THEN** reading the setting returns an empty list
- **AND** a later task exposes no owner-selectable skill

#### Scenario: Clearing restores the instance value

- **WHEN** an owner clears `OUROBOROS_USER_ENABLED_SKILL_IDS` after a task has already started
- **THEN** the clear invalidates that owner and agent's exact cache entry
- **AND** reading the setting returns the instance `AppSettings` value
- **AND** a later task uses that instance allowlist

#### Scenario: The value cannot declare a skill source

- **WHEN** a request attempts to put a package path, URL, payload, or credentials in the setting
- **THEN** none of those values creates or changes a provider, catalog record, or skill package
