# security-context Specification

## Purpose

The per-user security context: typed header and cookie parameters the browser transport records under the scope's security token, each with its own TTL, declared in the image, stored behind an interface whose backend a composition root installs, and never rendered to the model. Created by change add-mcp-auth.

## Requirements

### Requirement: The security context is keyed by the scope's security token

The system SHALL key what it records about a caller by `OwnerScope.security_token` — the value the scope
derives from its owner, agent and surface, never from its chat — and by nothing else. The store SHALL take
the token as a string handed to it and SHALL NOT compute one, the collector SHALL never receive it, and no
boundary SHALL accept a token from a request: the browser endpoint and the egress authorization of both
outbound clients — the MCP client and the A2A client — SHALL read it off the scope they hold. The A2A tool
provider SHALL hand the A2A client the scope of the run's tool context, and no client SHALL take a scope
from ambient state such as a context variable.

Because the token is derived from the scope a run already carries, nothing new SHALL be carried for it: the
inbound envelope, the task payload, `ToolContext` and every stored row SHALL keep their shapes. The
`security_contexts` table records credentials rather than work on an owner's behalf, so it SHALL carry the
token and no ownership axis columns; the axis-column rule of `scope-keyed-storage` does not apply to it. A run SHALL
find the context through its own scope, so a subtask, a retry, a recovered lease and a schedule firing find
the context of their owner, agent and surface exactly as the run that caused them did.

#### Scenario: Two chats of one owner, agent and surface share one context

- **WHEN** a context is saved under the token of a scope and later loaded under the token of a scope with
  the same owner, agent and surface but a different chat
- **THEN** the load returns the saved parameters

#### Scenario: Another agent of the same owner has no context

- **WHEN** a context is saved for owner `o` and agent `a` on the browser surface and loaded for owner `o`
  and agent `b` on the browser surface
- **THEN** the load returns an empty context

#### Scenario: Another surface of the same owner and agent has no context

- **WHEN** a context is saved under the token of owner `o`, agent `a` and the browser surface and loaded
  under the token of owner `o`, agent `a` and the A2A surface
- **THEN** the load returns an empty context

#### Scenario: A subtask resolves the context of its parent's scope

- **WHEN** a browser-originated run with a saved context requests a subtask and the subtask calls a
  tool of a server that forwards user credentials
- **THEN** the subtask's call carries the credentials saved under the parent's token

#### Scenario: A subtask delegating to an A2A agent resolves the context of its parent's scope

- **WHEN** a browser-originated run with a saved context requests a subtask and the subtask calls the tool
  of an A2A agent whose `user_headers` authorization names a saved header
- **THEN** the request sending the message to that agent carries the value saved under the parent's token

#### Scenario: The A2A client is handed the run's scope

- **WHEN** a run of owner `o` and agent `a` on the browser surface calls the tool of an A2A agent whose
  authorization is `user_headers`
- **THEN** the A2A client resolves the credentials of that call for the scope of the run's tool context
- **AND** the store is asked under that scope's security token and under no other

### Requirement: A context is a set of typed parameters, one value per key, each with its own TTL

A `SecurityContext` SHALL map parameter keys to values. A key SHALL be a parameter type and a name. The
parameter type SHALL be a closed string enumeration, `SecurityParameterType`, whose members today are
`header` and `cookie`; a new kind of recorded fact SHALL be added as a new member, never as a free string,
and constructing a key with a type outside the enumeration SHALL raise. A header name SHALL be lowercased
when the key is built; a cookie name SHALL be kept as sent, since cookie names are case-sensitive. A
context SHALL hold at most one value per key.

What a collector returns and a save takes SHALL be `SecurityParameter` values, each a key, a value and a
`ttl_sec`. Constructing a parameter whose `ttl_sec` is not a positive integer, or is a boolean, SHALL raise
naming the parameter's type and name and not its value. A backend that keeps what it saves SHALL refuse a
set holding two parameters of one key, naming the key, before it replaces anything.

#### Scenario: A header name is case-insensitive and a cookie name is not

- **WHEN** a context holds header `X-Sudir-Token` and cookie `Sudir`, and values are asked for header
  `x-sudir-token`, cookie `Sudir` and cookie `sudir`
- **THEN** the first two return their values and the third returns nothing

#### Scenario: An unknown parameter type is refused

- **WHEN** a parameter key is built with the type `certificate`
- **THEN** construction raises

#### Scenario: A non-positive or boolean TTL is refused

- **WHEN** a parameter is built with `ttl_sec` of `0`, `-1` or `True`
- **THEN** construction raises naming the parameter's type and name
- **AND** the message does not contain its value

#### Scenario: Two parameters of one key are refused

- **WHEN** the SQL store is asked to save header `X-Sudir-Token` and header
  `x-sudir-token` under one token
- **THEN** the save raises naming `header:x-sudir-token`
- **AND** the token's previously saved set is unchanged

### Requirement: What is collected is declared in the image

The system SHALL collect only the parameters declared by `security_config/auth/header_collector.yaml`,
which is part of the image and SHALL be parsed when `security_config/auth/header_collector.py` is imported,
so that a refused declaration stops the process from starting. The declaration SHALL be a mapping with
exactly one key, `parameters`, holding a list of entries with exactly the keys `type`, `name` and
`ttl_sec`:

- `type` SHALL be a member of `SecurityParameterType`;
- `name` SHALL be an HTTP token, and a `header` entry named `cookie` in any case SHALL be refused, since
  cookies are declared with type `cookie`;
- `ttl_sec` SHALL be a strict positive integer: a boolean, a string, a float, zero and a negative number
  SHALL be refused.

An unknown key at either level SHALL be refused. Two entries of one key SHALL be refused, header names
compared without regard to case and cookie names with regard to case; a header and a cookie MAY share a
name. An empty `parameters` list SHALL be accepted and collect nothing; a missing `parameters` key or an
empty file SHALL be refused. The parsing models SHALL live in `infra/security_context/` and SHALL NOT read
YAML or a path.

#### Scenario: The shipped declaration parses

- **WHEN** `security_config/auth/header_collector.py` is imported unchanged
- **THEN** it exports a declaration whose every parameter has a positive `ttl_sec`

#### Scenario: A malformed declaration is refused

- **WHEN** a declaration names an unknown type, carries a key `ttl`, names `X Sudir`, gives `ttl_sec` as
  `0`, `true`, `"3600"` or `1.5`, declares header `X-A` and header `x-a`, declares cookie `s` twice,
  declares a header named `Cookie`, lacks `parameters`, or is empty
- **THEN** parsing raises

#### Scenario: Cookie names differing in case, and a header and cookie of one name, are allowed

- **WHEN** a declaration names cookies `Sudir` and `sudir`, and a header and a cookie both named `sudir`
- **THEN** it parses

### Requirement: The browser surface records every accepted handshake through the installed collector

The system SHALL collect a context from request headers only through `ISecurityContextCollector`, whose
`collect` takes the handshake's `(name, value)` header pairs and returns a tuple of `SecurityParameter`,
through a holder a composition root initializes. The single-process server and the browser transport tier
SHALL install `DeclaredParametersCollector` built over the image's declaration. A root that serves no
browser WebSocket SHALL install no collector.

`DeclaredParametersCollector` SHALL collect a header only when its lowercased name is declared with type
`header` and the handshake carries that name exactly once with a non-empty value, recorded exactly as sent.
A declared header that occurs more than once — whatever its values, empty ones included — SHALL NOT be
recorded, because a repeated credential header is what a header injected along the path looks like, and
the collector SHALL log a warning naming the header and never its values. It SHALL NOT
record `cookie` as a header. Every `cookie` header SHALL instead be split on `;`, each chunk trimmed of
spaces and tabs only; a chunk without `=` or with an empty name SHALL be skipped; the first chunk naming a
cookie SHALL claim that name even when its value is empty; once every `cookie` header is read, a claimed
name with an empty value SHALL be dropped; and only names declared with type `cookie` SHALL be kept. A
cookie value SHALL be recorded exactly as sent — never unquoted — under the name as sent. The collected
parameters SHALL be returned in declaration order, each carrying its declared `ttl_sec`. A deployment SHALL
narrow collection by declaring fewer names, never by a second parser.

The browser WebSocket endpoint SHALL read the store and collector holders before recording, then collect
the handshake's headers and save the result under the `security_token` of the scope it resolved, after
identity resolution succeeds and before the connection is accepted. A save SHALL replace every parameter
the token held with the saved set.

A handshake refused by identity resolution SHALL record nothing. A collector or a save that fails SHALL
refuse the handshake with a stated reason and SHALL NOT accept the connection. Recording SHALL NOT depend on
which identity resolver is installed, and SHALL NOT be performed by the resolver.

The browser HTTP routes SHALL NOT record a context.

#### Scenario: An accepted handshake records its declared parameters

- **WHEN** the declaration names header `X-Sudir-Token` and cookies `sudir` and `theme`, a client completes
  the WebSocket handshake with header `X-Sudir-Token: t1` and `Cookie: sudir=abc; theme=dark`, and identity
  resolution accepts it
- **THEN** the store holds, under the token of the resolved scope, header `x-sudir-token` with `t1`,
  cookie `sudir` with `abc` and cookie `theme` with `dark`, each with its declared TTL
- **AND** holds no `cookie` header

#### Scenario: An undeclared header or cookie is not recorded

- **WHEN** the declaration names only header `X-Sudir-Token`, and a handshake carries that header,
  `Authorization` and `Cookie: sudir=abc`
- **THEN** the store holds only header `x-sudir-token` under the resolved scope's token

#### Scenario: Parameters follow the declaration's order and TTLs

- **WHEN** the declaration names cookie `sudir` with `ttl_sec` 60 and then header `X-A` with `ttl_sec` 600,
  and a handshake carries both, the header first
- **THEN** the collector returns cookie `sudir` with TTL 60 and then header `x-a` with TTL 600

#### Scenario: A repeated header is not recorded

- **WHEN** the declaration names headers `X-Sudir-Token` and `Authorization`, and the handshake carries
  `X-Sudir-Token: t1`, `Authorization: Bearer b` and `X-Sudir-Token: t2`
- **THEN** the collector returns header `authorization` with `Bearer b`
- **AND** returns no header `x-sudir-token`
- **AND** logs a warning naming `x-sudir-token` and neither `t1` nor `t2`

#### Scenario: A header sent once is recorded raw

- **WHEN** the declaration names header `X-Sudir-Token` and the handshake carries it once with
  ` Bearer t1, t2 `
- **THEN** the collector returns header `x-sudir-token` with ` Bearer t1, t2 ` unchanged

#### Scenario: A repeated cookie is claimed by its first occurrence

- **WHEN** the declaration names cookie `sudir` and the handshake's cookie headers hold
  `sudir="a b"; sudir=later`, or `sudir=; sudir=later`
- **THEN** the first records cookie `sudir` with `"a b"`
- **AND** the second records no cookie `sudir`

#### Scenario: A newer connection replaces the whole set

- **WHEN** two connections of the same owner, agent and surface complete their handshakes one after
  another, the first with declared headers `x-a` and `x-b` and the second with `x-a` only
- **THEN** a load returns the second connection's `x-a` and no `x-b`

#### Scenario: A refused handshake records nothing

- **WHEN** identity resolution refuses a handshake
- **THEN** no context is saved

#### Scenario: A failed collection or save refuses the connection

- **WHEN** the collector or the store raises while the handshake's context is being recorded
- **THEN** the connection is closed with a stated reason and never accepted
- **AND** nothing is saved when the collector raised

#### Scenario: A history request records nothing

- **WHEN** a client requests chat history over HTTP with headers
- **THEN** no context is saved

### Requirement: Only surfaces that record a context resolve one

A run SHALL be given stored parameters only when its scope's surface is one whose transport records
contexts, whichever outbound client asks for them: an MCP server or an A2A agent whose authorization is
`user_headers`. Today that set SHALL be exactly the browser surface. A run whose scope carries the A2A, the
skill-bridge or the dialogs surface SHALL be refused before the store is read, with a reason naming the
surface, and SHALL send no request to the server or agent. Its token already differs from the browser
surface's; the refusal is kept so that such a run is told why, rather than being told a declared name is
missing.

A run whose scope carries the browser surface SHALL resolve the context saved under its token whoever
minted that scope. In particular a run an extension injected with the browser as its delivery source,
under the configured owner and agent, SHALL resolve the context a browser handshake saved for that owner
and agent.

#### Scenario: An A2A-originated run does not see a browser user's headers

- **WHEN** a context is saved from a browser handshake for owner `o` and agent `a`, and a run with the
  same owner and agent but the A2A surface asks for it
- **THEN** the run is refused naming the surface
- **AND** the store is not read

#### Scenario: A delegation from a non-recording surface does not see a browser user's headers

- **WHEN** a context is saved from a browser handshake for owner `o` and agent `a`, and a run with the same
  owner and agent on the dialogs, the skill-bridge or the A2A surface calls the tool of an A2A agent whose
  authorization is `user_headers`
- **THEN** the tool result is an `⚠️ A2A_ERROR` stating that the call to that agent cannot be authorized and
  naming the run's surface
- **AND** the store is not read
- **AND** no request is sent to the agent

#### Scenario: A browser-originated run sees the context

- **WHEN** a run with the browser surface asks for the parameters saved under its token
- **THEN** it receives them

#### Scenario: An extension injecting for the browser inherits the browser user's context

- **WHEN** the configured owner's browser handshake saved a context, and an extension injects a message
  with delivery source `browser` whose run calls a tool of a server forwarding user credentials
- **THEN** that call carries the credentials the handshake saved

#### Scenario: An extension injecting for itself does not

- **WHEN** an extension injects a message with no delivery source and its run calls the same tool
- **THEN** the call is refused for lack of user credentials

### Requirement: The runtime renders no stored parameter value

A parameter value held in a `SecurityParameter` or a `SecurityContext` SHALL NOT appear in anything the
runtime itself renders: the `repr` and `str` of a parameter or a context, an authorization refusal, a log
record or exception message the runtime writes, an event, a trace payload, a task payload or a stored row
outside the store's own sealed column. The `repr` and `str` of a context SHALL show each parameter's type
and name only, and those of a parameter its type, name and TTL only.

The only path by which the runtime sends a value out SHALL be an outbound request header of an entry whose
`user_headers` authorization declared that name — an MCP server of `security_config/mcp.py` or an A2A agent
of `security_config/a2a.py` — on a request whose scheme, host and port, a default port normalized, equal
those of that entry's configured URL. A value SHALL ride only on the requests of the call whose run
resolved it — the request carrying one MCP `tools/call`, or the requests of one A2A operation — and never as
a header of a client shared by several owners. No request a client originates on its own — MCP
negotiation, discovery or keepalive, or A2A agent card discovery — SHALL carry one.

Text an MCP server or an A2A agent returns as a result SHALL be relayed as it sent it; whether it echoes a
credential is a property of that server or agent. An A2A agent's rejection of the credentials — an HTTP
`401` or `403`, or a task it puts in the `auth-required` state — SHALL be reported without the HTTP
client's or the protocol SDK's text.

#### Scenario: The representation of a context omits values

- **WHEN** a context holding header `authorization` and cookie `sudir` is rendered with `repr` and `str`
- **THEN** the output names `header:authorization` and `cookie:sudir` and contains neither value

#### Scenario: The representation of a parameter omits its value

- **WHEN** a parameter for header `X-Sudir-Token` with TTL 3600 is rendered with `repr` and `str`
- **THEN** the output is `SecurityParameter(header:x-sudir-token, ttl_sec=3600)`

#### Scenario: A refused call does not echo a value

- **WHEN** an MCP call is refused because a declared cookie is missing from the stored parameters
- **THEN** the tool result names the server and the cookie name
- **AND** contains no header or cookie value from the context

#### Scenario: A refused delegation does not echo a value

- **WHEN** a browser-originated run calls the tool of an A2A agent declaring header `X-Sudir-Token` and cookie
  `sudir`, and the stored parameters hold the header but lack the cookie
- **THEN** no request is sent to the agent
- **AND** the tool result is an `⚠️ A2A_ERROR` naming the agent and the cookie name
- **AND** neither the tool result nor any log record written during the call contains the stored header value

#### Scenario: A rejected delegation does not echo a value

- **WHEN** a browser-originated run whose context holds header `x-sudir-token` calls the tool of an A2A agent
  declaring that header, and the agent answers the request with HTTP `401`
- **THEN** the tool result is an `⚠️ A2A_ERROR` naming the agent
- **AND** neither the tool result nor any log record written during the call contains the stored value

#### Scenario: Agent card discovery carries no stored value

- **WHEN** the A2A client fetches the agent card of an agent whose authorization is `user_headers`
- **THEN** the discovery request carries no header the entry declares and no `Cookie`

#### Scenario: A stored value reaches only its entry's origin

- **WHEN** an A2A agent declaring header `X-Sudir-Token` publishes a card listing a JSON-RPC interface on its
  configured origin and one on another origin, or answers a request with a redirect to another origin, and a
  browser-originated run whose context holds that header calls its tool
- **THEN** every request carrying the stored value is addressed to the configured origin
- **AND** no request to the other origin carries it

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
test suite. `OUROBOROS_CHAT_ENCRYPTION_KEY` SHALL be mandatory for those roots: a root without a valid key
SHALL refuse to start before it serves, with an error naming the setting and never rendering the key.

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
  unusable `OUROBOROS_CHAT_ENCRYPTION_KEY`
- **THEN** composition raises naming `OUROBOROS_CHAT_ENCRYPTION_KEY` and not the value
- **AND** no store is installed

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
with the async engine and `OUROBOROS_CHAT_ENCRYPTION_KEY`, neither of which has a default, and with no TTL
of its own, and SHALL refuse construction when the key is not 32 bytes of valid base64, without rendering
the key.

The token, the parameter type and the name SHALL be stored in the clear, so that a load selects only the
requested keys. Every value SHALL be encrypted with AES-256-GCM under that key, on every database dialect
and whether or not message field encryption is enabled, with the token, the type and the name as
associated data, so that a ciphertext moved to another token, type or name does not decrypt. No owner,
agent or surface id SHALL be stored.

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

- **WHEN** the store is constructed with an empty, non-base64 or 16-byte key
- **THEN** construction raises naming `OUROBOROS_CHAT_ENCRYPTION_KEY` and not the value
