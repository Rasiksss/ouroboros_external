## MODIFIED Requirements

### Requirement: The store is an interface whose backend a composition root installs

The system SHALL reach the store only through `ISecurityContextStore`, with an asynchronous
`save(token, parameters)` that replaces the token's whole set and an asynchronous `load(token, keys)` that
returns a context holding only the requested keys the token has whose TTL has not passed — an empty
context when it has none — through a holder a composition root initializes. The same instance SHALL serve
the browser endpoint of a process that has one, and SHALL be handed at initialization, never per call, to
both outbound clients of a process that has them, as the `security_context_store` of the one
`EgressAuthDependencies` value the root builds: to the MCP client directly and to the A2A client inside its
`A2aEgressDependencies`. The authenticators of both clients SHALL reach the store only through that value.
Reading the store holder or the collector holder before a composition root initialized it SHALL raise
rather than fall back to a backend. Initializing a holder again SHALL replace what it held.

Neither holder SHALL offer a method that returns it to its uninstalled state; a test that needs an
uninstalled holder SHALL replace the reference its consumer module reads.

Every composition root that installs the store — the single-process server, the browser transport tier
and the agent tier — SHALL install the SQL backend over the runtime database, whatever the multi-user mode
says, after its async engine is initialized. No in-process or discarding backend SHALL exist outside the
test suite. The root SHALL compose the store from `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` and never from
`OUROBOROS_CHAT_ENCRYPTION_KEY`, which SHALL play no part in installing it. The credentials key SHALL be
mandatory for those roots: a root without a valid key SHALL refuse to start before it serves, with an error
naming the setting and never rendering the key.

#### Scenario: A load returns only the requested keys

- **WHEN** a set holding header `x-sudir-token`, header `authorization` and cookie `sudir` is saved, and a
  load asks for header `x-sudir-token` and cookie `missing`
- **THEN** the returned context holds header `x-sudir-token` alone

#### Scenario: Each parameter expires after its own TTL

- **WHEN** header `x-a` with TTL 60 and header `x-b` with TTL 600 are saved together, and a load for both
  runs 120 seconds later
- **THEN** the returned context holds `x-b` alone

#### Scenario: A parameter is not served at its expiry

- **WHEN** a parameter with TTL 60 is saved and loaded exactly 60 seconds later
- **THEN** the load returns an empty context

#### Scenario: The server installs the SQL backend

- **WHEN** the single-process server composes the security context and accepts a handshake
- **THEN** the installed store is the SQL backend
- **AND** a second SQL store over the same database and key loads the handshake's parameters

#### Scenario: The transport tier records what the agent tier reads

- **WHEN** the browser transport tier of the multi-user mode accepts a handshake
- **THEN** its installed store is the SQL backend
- **AND** a SQL store over the same database and key, as the agent tier installs, loads the handshake's
  parameters

#### Scenario: Both outbound clients are handed the one installed store

- **WHEN** the single-process server or the agent tier composes with `MCP_ENABLED` and `A2A_EGRESS_ENABLED`
  on and a non-empty list of each
- **THEN** the MCP client and the A2A client are initialized with the same `EgressAuthDependencies` value,
  the A2A client's inside its `A2aEgressDependencies`
- **AND** that value's `security_context_store` is the store the root installed

#### Scenario: A root without a valid key refuses to start

- **WHEN** the single-process server, the browser transport tier or the agent tier composes with an empty or
  unusable `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, including a valid 32-byte chat encryption key copied into
  it, while `OUROBOROS_CHAT_ENCRYPTION_KEY` is valid
- **THEN** composition raises naming `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` and not the value
- **AND** no store is installed

#### Scenario: A root installs the store without the chat encryption key

- **WHEN** the single-process server, the browser transport tier or the agent tier composes with a valid
  `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, message field encryption off and `OUROBOROS_CHAT_ENCRYPTION_KEY`
  empty
- **THEN** the installed store is the SQL backend

#### Scenario: An uninstalled store or collector is loud

- **WHEN** the browser endpoint reads the store holder or the collector holder before a composition root
  initialized it
- **THEN** it raises naming the missing initialization

#### Scenario: A holder offers no reset

- **WHEN** the store holder and the collector holder are inspected
- **THEN** neither has `reset_for_tests`
- **AND** initializing either again replaces the backend or collector it held

#### Scenario: A restart keeps every unexpired context

- **WHEN** the process restarts and a derived task loads the parameters of a token whose TTLs have not passed
- **THEN** the load returns them

### Requirement: The SQL backend stores one sealed row per parameter and serves it only within its TTL

The system SHALL provide a SQL backend of `ISecurityContextStore` over the runtime database, holding one
row per token, parameter type and name, with the parameter's value and its expiry. It SHALL be constructed
with the async engine and `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, neither of which has a default, and with no
TTL of its own, and SHALL refuse construction when the key, with whitespace removed, is not exactly 64 bytes
of valid base64, without rendering the key. A 32- or 48-byte key SHALL be refused although AES-SIV has
variants for it.

The token, the parameter type and the name SHALL be stored in the clear, so that a load selects only the
requested keys. Every value SHALL be sealed with AES-256-SIV (RFC 5297) under that key, used as the whole
512-bit SIV key, on every database dialect and independently of message field encryption: a fresh random
16-byte nonce per value, the associated-data vector the JSON array of the token, the type and the name
followed by that nonce, and the stored text the URL-safe base64 of the nonce, the synthetic IV and the
ciphertext in that order. A ciphertext moved to another token, type or name SHALL NOT decrypt, two saves of
one value SHALL store different ciphertexts, and a value sealed under `OUROBOROS_CHAT_ENCRYPTION_KEY`, as
values were before the credentials key existed, SHALL NOT open. No owner, agent or surface id SHALL be
stored.

A save SHALL refuse two parameters of one key before touching any row. It SHALL then, in one transaction,
write one row per saved parameter expiring that parameter's own TTL after the
save — replacing a row of the same key, including one a concurrent save of the token committed — and remove
every other row of the token; saving an empty set SHALL leave the token with no rows. It SHALL touch no row
of another token, expired or not, so an expired row SHALL be removed by nothing but a later save of its own
token; a load SHALL never serve one. Two concurrent saves
of one token SHALL serialize against each other, so that neither fails and the token is left holding exactly
one of the two sets. A load
with no requested keys SHALL return an empty context without querying. A load SHALL select only unexpired
rows of the requested keys, SHALL decrypt only those, and SHALL raise, never return an empty context, for a
selected row it cannot decrypt.

#### Scenario: A context round-trips through the database

- **WHEN** a set holding a header and a cookie is saved and loaded for both keys before their TTLs pass
- **THEN** the load returns both values

#### Scenario: Every row expires after its own TTL

- **WHEN** a header with TTL 60 and a cookie with TTL 600 are saved together
- **THEN** the header's row expires between 60 seconds after the save's start and 60 seconds after its end
- **AND** the cookie's row between 600 seconds after the save's start and 600 seconds after its end

#### Scenario: A save with two parameters of one key touches no row

- **WHEN** a token holds rows and a save for it carries two parameters of one key
- **THEN** the save raises
- **AND** every row stored before it is unchanged

#### Scenario: Two concurrent saves of one token leave one save's whole set

- **WHEN** two handshakes of one token save different sets at the same time, one carrying a header the
  other carries beside a cookie
- **THEN** both saves succeed
- **AND** a load returns exactly one of the two sets, never a row of each

#### Scenario: A save replaces the token's set

- **WHEN** a set holding headers `x-a` and `x-b` is saved, then a set holding `x-a` alone, and a load asks
  for both
- **THEN** the load returns `x-a` from the second save and no `x-b`

#### Scenario: The database holds no value and no owner in the clear

- **WHEN** a set holding an `x-sudir-token` header and a cookie is saved for owner `alice`
- **THEN** each stored row carries the parameter's name in the clear
- **AND** no stored row contains either value or `alice`

#### Scenario: An expired parameter loads nothing

- **WHEN** a set was saved and a parameter's TTL has passed
- **THEN** a load for that parameter's key returns an empty context

#### Scenario: An expired row stays stored until its own token saves again

- **WHEN** a token's rows have expired and no further set is saved for it
- **THEN** the rows are still stored
- **AND** a load for their keys returns an empty context

#### Scenario: A save leaves another token's expired rows alone

- **WHEN** another token's rows have expired and a set is saved
- **THEN** the other token's rows are stored unchanged

#### Scenario: A ciphertext moved to another token, name or type does not open

- **WHEN** the stored ciphertext of one row is copied onto a row of another token, onto a row of another
  name, or onto a row of another type, and that row is loaded
- **THEN** the load raises

#### Scenario: An unreadable row that is not requested does not break a load

- **WHEN** one row of a token cannot be decrypted and a load asks only for another key of that token
- **THEN** the load returns the requested value

#### Scenario: An unusable key is refused at construction

- **WHEN** the store is constructed with an empty, non-base64, 16-, 32- or 48-byte key
- **THEN** construction raises naming `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` and not the value

#### Scenario: A key broken over lines is accepted

- **WHEN** the store is constructed with the base64 of a 64-byte key split over two lines
- **THEN** a set saved through it loads back

#### Scenario: A stored value is AES-256-SIV with its nonce in front

- **WHEN** a value is saved and its stored text is decoded
- **THEN** AES-256-SIV under the key opens the bytes after the first 16, given the JSON array of the token,
  the type and the name followed by those first 16 bytes as associated data

#### Scenario: A value sealed under the chat encryption key does not open

- **WHEN** a row's value was sealed with AES-256-GCM under a chat encryption key with the row's associated
  data, as before the credentials key existed, and that row is loaded
- **THEN** the load raises
