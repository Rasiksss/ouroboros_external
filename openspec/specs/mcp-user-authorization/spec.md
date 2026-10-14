# mcp-user-authorization Specification

## Purpose

How an MCP tool call carries the user's credentials: each declared server names exactly one authorization kind (`none`, `user_headers`, `user_token`, `mtls`), only image-declared header and cookie names are forwarded, per-call credentials ride only on one call's request over streamable HTTP to the server's own origin, and a call that needs user credentials and has none fails closed. Created by change add-mcp-auth.
## Requirements
### Requirement: A server declares exactly one authorization

`McpServerEntry` SHALL declare its authorization as data in `auth`, a union discriminated by `kind`,
defaulting to `none`. The kinds SHALL be the members of one string enumeration — `none`, `user_headers`,
`user_token` and `mtls` — and every declaration shape SHALL derive from one base that declares `kind`.
The enumeration, the base and the union SHALL be the egress authorization kinds — `EgressAuthKind`,
`EgressAuthBase` and `EgressAuth` — declared once in the settings schema and shared with the `auth` of an
A2A remote-agent entry. No MCP-specific or A2A-specific copy of a kind or of its validation SHALL exist, and
the wire values of the kinds SHALL be unchanged. Nothing else on the entry SHALL authorize: the entry SHALL
have no top-level `auth_header`, `auth_token` or `tls`, and an entry carrying one SHALL be refused as carrying
an unknown key. No entry SHALL carry a list of authorizations to try in turn. An unknown key inside `auth`
SHALL be refused.

The declaration SHALL be serializable and SHALL be importable without importing `ouroboros.mcp` or
`ouroboros.egress`. The DEV coercion path SHALL name a refused entry without rendering a token it carries.

Every kind of the union SHALL map to exactly one authenticator implementation through one mapping, built
once per connection from the entry, the user token target kind the entry is — an MCP server — and the
authorization dependencies — the security context store and the user token store — the MCP client was
initialized with; a new kind SHALL be added as one enumeration member, one config shape, one authenticator
and one mapping entry.

The mapping (`authenticator_for`), the authenticator interface (`IEgressAuthenticator`) and its
implementations, the surface gates, the authorization refusal (`EgressAuthRefused`), the authorization
dependencies value (`EgressAuthDependencies`), the static credential headers and the credentialed-client
guard SHALL be one egress authorization layer, `ouroboros/egress/auth/`, consumed by both the MCP and the
A2A client. The layer SHALL
import neither client (`ouroboros.mcp`, `ouroboros.egress.a2a`), no transport, supervisor, multi-user tier,
agent or tool catalogue package (`transport`, `supervisor`, `multiuser_config`, `ouroboros.agent`,
`new_core.tools`), and neither protocol SDK (`mcp`, `a2a`). No MCP-specific module or alias of the layer
SHALL remain. Sharing the layer SHALL change neither the behaviour of an MCP call nor the text of any MCP
refusal.

#### Scenario: An entry without auth declares none

- **WHEN** an entry with only `id` and `url` is constructed
- **THEN** it is accepted and its `auth` kind is `none`

#### Scenario: The earlier top-level fields are refused

- **WHEN** an entry declares a top-level `auth_header` and `auth_token`, or a top-level `tls`
- **THEN** construction fails naming the unknown key
- **AND** the DEV coercion path drops and names the entry without rendering the token value

#### Scenario: A misspelled field is refused

- **WHEN** an entry declares `auth` of kind `user_headers` with a key `cookie` instead of `cookies`
- **THEN** construction fails naming the unknown key

#### Scenario: Every kind has an authenticator

- **WHEN** the members of the authorization union and of the kind enumeration are enumerated
- **THEN** each union member derives from the base, the two sets of kinds are equal, and each member maps
  to an authenticator implementation

#### Scenario: Every kind round-trips through the DEV settings

- **WHEN** an entry of each kind is serialized and parsed back
- **THEN** the parsed entry equals the original

#### Scenario: The kinds are shared with A2A remote-agent entries

- **WHEN** the `auth` field of `McpServerEntry` and of `A2aRemoteAgentEntry` is inspected
- **THEN** both are the one `EgressAuth` union over `EgressAuthKind`
- **AND** the values of `EgressAuthKind` are exactly `none`, `user_headers`, `user_token` and `mtls`

#### Scenario: The declaration imports no client

- **WHEN** the settings schema is imported in a fresh interpreter
- **THEN** no module under `ouroboros.mcp` or `ouroboros.egress` is imported

#### Scenario: The shared layer imports no client, transport, tier or protocol SDK

- **WHEN** the modules of `ouroboros/egress/auth/` are imported in a fresh interpreter and their import
  statements are read
- **THEN** none of `ouroboros.mcp`, `ouroboros.egress.a2a`, `transport`, `supervisor`, `multiuser_config`,
  `ouroboros.agent`, `new_core.tools`, `mcp` or `a2a` is imported
- **AND** no module `ouroboros.mcp.auth` can be found

### Requirement: Only declared, unprotected names are forwarded

A `user_headers` authorization SHALL name at least one request header or cookie. Names SHALL be HTTP
tokens and unique regardless of case. A `user_token` authorization SHALL name its `auth_header`, an HTTP
token. An entry SHALL be refused when `user_headers.headers` or `user_token.auth_header` names, compared
without regard to case, `host`, `content-length`, `content-type`, `transfer-encoding`, `connection`,
`keep-alive`, `proxy-connection`, `proxy-authorization`, `te`, `trailer`, `upgrade`, `expect`, `accept`,
`cookie`, `last-event-id`, the trace identifier header, the agent identifier header, or any name
beginning with `mcp-`, `sec-websocket-`, `a2a-` or `x-a2a-`. Cookies SHALL be declared in `cookies`.

The protected names SHALL be refused by the shared authorization kinds themselves, so the refusal SHALL
hold identically for an MCP server entry and an A2A remote-agent entry, and no forwarded name SHALL
override an A2A protocol header such as `A2A-Version`, `A2A-Extensions` or `X-A2A-Extensions`.

For `user_headers` the authenticator SHALL load from the security context store, under the run's security
token, exactly the parameters the entry declares — each name in `headers` as a `header` parameter and each
name in `cookies` as a `cookie` parameter — and nothing else. Each declared header SHALL be sent with the
value recorded for it, which the collector recorded only from a header the browser sent exactly once. The declared
cookies SHALL be sent as one `Cookie` header holding only them, in declared order, each `name=value` with
the value exactly as the browser sent it, which the collector already took from the first occurrence of the
name. A declared name with no unexpired value recorded, or with an empty one, SHALL be treated as a missing
header or cookie. A forwarded value that a request header cannot hold — a character outside horizontal tab,
space and visible ASCII, or a space or tab at either end — SHALL be treated as absent credentials: the call
is refused and the value is not rendered.

No header or cookie the entry does not name SHALL be loaded or forwarded from the context.

#### Scenario: Only the declared parameters are requested from the store

- **WHEN** an entry declares header `X-Sudir-Token` and cookies `sudir` and `lang`, and a browser-originated
  run calls one of its tools
- **THEN** the store is asked, under the run's security token, for exactly header `x-sudir-token`, cookie
  `sudir` and cookie `lang`

#### Scenario: A protected header name is refused

- **WHEN** an entry declares `user_headers` with `headers` containing `X-Trace-Id`, `Mcp-Session-Id`,
  `Last-Event-ID` or `Sec-WebSocket-Key`, or `user_token` with `auth_header` `Host`
- **THEN** construction fails naming the protected header

#### Scenario: An A2A protocol header cannot be forwarded

- **WHEN** an MCP server entry or an A2A remote-agent entry declares `user_headers` with `headers`
  containing `A2A-Version`, `a2a-extensions` or `X-A2A-Extensions`, or `user_token` with `auth_header`
  `A2A-Version`
- **THEN** construction fails naming the protected header

#### Scenario: A user token without a valid header name is refused

- **WHEN** an entry declares `user_token` without `auth_header`, or with `auth_header` `X API Key`
- **THEN** construction fails

#### Scenario: Only the declared cookie is forwarded

- **WHEN** the handshake's `cookie` header held `sudir=abc; theme=dark`, both are declared for collection,
  and the entry declares cookie `sudir`
- **THEN** the `tools/call` request carries `Cookie: sudir=abc`
- **AND** does not carry `theme`

#### Scenario: A quoted and repeated cookie is forwarded as sent

- **WHEN** the handshake's `cookie` headers held `sudir="a b"; sudir=later` and the entry declares cookie
  `sudir`
- **THEN** the credentials carry `Cookie: sudir="a b"`

#### Scenario: A value a request header cannot hold means no request

- **WHEN** the context holds a declared header whose value contains CR LF, a byte outside ASCII, or a
  leading space
- **THEN** no `tools/call` request is sent
- **AND** the tool error names the header and contains no part of the value

#### Scenario: An undeclared header is not forwarded

- **WHEN** the context holds `authorization` and `x-sudir-token` and the entry declares only
  `x-sudir-token`
- **THEN** the `tools/call` request carries `X-Sudir-Token` from the context
- **AND** does not carry the context's `authorization` value

### Requirement: Every forwarded header or cookie is declared for collection

The single-process server, and under the multi-user mode its declared refusal set in every tier, SHALL
refuse to start when the MCP kill switch is on and an enabled
`user_headers` entry names a header or a cookie that the image's header collector declaration does not
declare with that type, since such a name would never be recorded and every call would be refused. A
disabled entry SHALL NOT be examined. The refusal SHALL name every undeclared name as the entry id followed
by `type:name`, a header name lowercased, and SHALL render no URL or token.

#### Scenario: An undeclared forwarded header stops the server

- **WHEN** the declaration names header `X-Sudir-Token` and cookie `sudir_session`, and the enabled list holds
  `hr` declaring `user_headers` with header `X-Other`
- **THEN** the server refuses to start naming `hr header:x-other`
- **AND** under the multi-user mode the mode's refusal set names `hr header:x-other`

#### Scenario: The shipped example forwards only declared names

- **WHEN** the undeclared names of the `EXAMPLE` polygon's list are computed against the shipped declaration
- **THEN** there are none

### Requirement: A user token authorizes a call with the personal token, else the static one

A `user_token` authorization SHALL declare `auth_header` and MAY declare a static `auth_token`, which SHALL be
treated as absent when empty, SHALL be refused when a request header cannot hold it without rendering it,
and SHALL NOT appear in the entry's `repr`.

Over `streamable_http`, the authenticator SHALL resolve a call's `auth_header` value in this order:

1. When the run's surface is one on which a personal token is looked up — every surface except A2A — the
   user token store SHALL be asked for the token of the MCP server target — target kind `mcp-server` and
   the entry's id — and the run's owner and agent. When the store raises, the call SHALL be refused without
   falling back to the static token, and only the error's type SHALL be logged. When it returns a non-empty
   token, the call SHALL carry it; a token a request header cannot hold SHALL refuse the call without
   falling back and without rendering it. An empty token SHALL count as absent.
2. Otherwise, when the entry declares a static `auth_token`, the call SHALL carry it.
3. Otherwise the call SHALL be refused: on the A2A surface stating that no user token is looked up on
   surface `a2a` and the server declares no static `auth_token`; elsewhere stating that no user token is
   stored for this owner and agent and the server declares no static `auth_token`.

A personal token stored for an A2A agent target SHALL never be read for, or carried by, a call to an MCP
server, whatever the ids. The authenticator is shared with the A2A client; for an MCP server its refusals
SHALL keep the texts above unchanged.

A run on the A2A surface SHALL NOT cause the user token store to be read.

On `streamable_http` the static token SHALL also be sent as a header of the connection's HTTP client, so
negotiation, discovery, keepalive and the standalone stream carry it; a personal token SHALL replace it on
the request of the call that resolved it and on no other request.

#### Scenario: A personal token wins over the static token

- **WHEN** a browser-originated run of owner `o` and agent `a` calls a tool of server `s` declaring
  `auth_header` `X-API-Key` and a static token, and the store holds a personal token for `s`, `o` and `a`
- **THEN** the `tools/call` request carries `X-API-Key` with the personal token as its only value

#### Scenario: The static token is the fallback

- **WHEN** the store holds no personal token for the run's owner and agent
- **THEN** the `tools/call` request carries `X-API-Key` with the static token

#### Scenario: The store is read under the MCP server target

- **WHEN** the store holds a personal token for the A2A agent target with id `s` of owner `o` and agent `a`
  and none for the MCP server target `s`, and a browser-originated run of `o` and `a` calls a tool of MCP
  server `s` declaring a static token
- **THEN** the store is asked only for the target of kind `mcp-server` and id `s`
- **AND** the `tools/call` request carries the static token and no part of the A2A agent's token

#### Scenario: The MCP refusal still names the server

- **WHEN** a browser-originated run calls a tool of a `user_token` server over `streamable_http` without a
  static token and the store holds no personal token for the run's owner and agent
- **THEN** no `tools/call` request is sent
- **AND** the tool error states that no user token is stored for this owner and agent and the server
  declares no static `auth_token`

#### Scenario: Discovery carries the static token only

- **WHEN** the connection to a `user_token` server with a static token negotiates and lists its tools
- **THEN** those requests carry the static token
- **AND** carry no personal token

#### Scenario: Without a static token discovery is anonymous

- **WHEN** the connection to a `user_token` server without a static token lists its tools
- **THEN** no listing request carries `auth_header`

#### Scenario: An A2A run never reads the store

- **WHEN** a run on the A2A surface calls a tool of a `user_token` server with a static token
- **THEN** the call carries the static token
- **AND** the user token store is not read

#### Scenario: An A2A run without a static token is refused

- **WHEN** a run on the A2A surface calls a tool of a `user_token` server without a static token
- **THEN** no `tools/call` request is sent and the error names surface `a2a`

#### Scenario: The lookup covers every other surface

- **WHEN** the set of surfaces on which a personal token is looked up is compared with every surface
- **THEN** exactly the A2A surface is missing from it
- **AND** a run on the dialogs or the skill-bridge surface asks the store for its own owner and agent

#### Scenario: A store failure does not fall back

- **WHEN** the user token store raises while a call resolves its token and the server declares a static token
- **THEN** no `tools/call` request is sent and the tool error states the store is unavailable

#### Scenario: An unforwardable personal token does not fall back

- **WHEN** the store returns a personal token containing CR LF and the server declares a static token
- **THEN** no `tools/call` request is sent
- **AND** the tool error contains no part of either token

#### Scenario: Concurrent calls carry each owner's token

- **WHEN** runs of two owners, one with a personal token and one without, call the same server concurrently
- **THEN** one `tools/call` carries the first owner's personal token and the other the static token

#### Scenario: The static token is not rendered

- **WHEN** an entry declaring a static `auth_token` is rendered with `repr`
- **THEN** the output does not contain the token

### Requirement: Per-call credentials ride only on the request of one tool call

A connection SHALL be credentialed when its authenticator may put any header on a call: a `user_headers`
entry, or a `user_token` entry over `streamable_http`. The runtime SHALL keep one process-wide connection
per server and SHALL apply per-call credentials to the HTTP request that carries one `tools/call` — and to
no other request: not the call's cancellation notification, not a resumption request, not a reply the
client sends to a server request, and not a same-origin redirect's `Cookie`, which the HTTP client rebuilds
from its empty jar. The credentials SHALL be resolved inside the call, on the MCP client loop, within the
tool's timeout, for the scope of the run that made the call, by an authenticator the connection built when
it was created. They SHALL NOT be carried by ambient state such as a context variable.

Requests a credentialed connection originates on its own — negotiation, initialization, tool discovery,
keepalive and any standalone stream — SHALL carry no per-call credential. A call on a credentialed
connection SHALL NOT cause a tool listing to be sent from inside the call: it SHALL wait until the session's
listing completes, and SHALL be refused as a tool error, with no request, for a tool the current listing
does not contain. The wait SHALL end when the listing completes, when the session that was listing is
retired, or when the tool's budget runs out, whichever comes first.

A credentialed connection SHALL retain no cookie a response sets, so that no cookie set in answer to one
run's call reaches another run's call or the connection's own traffic. Two concurrent calls from runs of
different owners SHALL each carry only their own run's credentials.

#### Scenario: A tool call carries the declared header

- **WHEN** a browser-originated run whose context holds `x-sudir-token: t1` calls a tool of a server
  declaring that header
- **THEN** the HTTP request carrying `tools/call` has `X-Sudir-Token: t1`

#### Scenario: Discovery carries no forwarded user header

- **WHEN** the connection to a `user_headers` server lists tools or negotiates its protocol era
- **THEN** no request it sends carries a header or cookie declared by the server's `user_headers`

#### Scenario: A call during a reconnect's listing sends no credentialed listing

- **WHEN** a credentialed connection reconnects, its listing is delayed, and a run calls a tool of the
  server during that listing
- **THEN** no `tools/list` request carries a per-call credential
- **AND** the call proceeds once the listing completes, within its timeout

#### Scenario: A call waiting on a listing that fails gives up

- **WHEN** a run's call is waiting on a reconnect's listing and that listing fails
- **THEN** the call returns a not-connected tool error without waiting out its budget
- **AND** no `tools/call` request is sent

#### Scenario: The cancellation of a call carries no credential

- **WHEN** a credentialed `tools/call` times out and the client sends its cancellation
- **THEN** the cancellation request carries no forwarded header or cookie and no personal token

#### Scenario: A server-set cookie does not cross owners

- **WHEN** a credentialed server answers owner A's `tools/call` with `Set-Cookie: session=s1`
- **THEN** owner B's next `tools/call`, and the connection's discovery and keepalive requests, carry no
  `session` cookie

#### Scenario: Concurrent calls do not mix owners

- **WHEN** runs of two owners with different contexts call the same `user_headers` server concurrently
- **THEN** each `tools/call` request carries only the header value of its own run's context

#### Scenario: A connection that forwards nothing is not credentialed

- **WHEN** an entry declares `none`, `mtls`, or `user_token` over `sse`
- **THEN** its authenticator forwards no header, so its client keeps the default cookie handling and no
  per-call wrapper is installed
- **AND** a `user_token` entry's static `auth_header` is still removed from any request to another origin

#### Scenario: Trace headers are unaffected

- **WHEN** a tool call carries user credentials
- **THEN** it also carries the active trace identifier and the agent identifier as before

### Requirement: A call that needs user credentials and has none fails closed

When a server declares `user_headers`, the call SHALL NOT be sent, and SHALL return a formatted MCP tool
error that the loop classifies as a tool failure, when any of these hold: the run's surface records no
context, the store holds no non-empty unexpired value under the run's security token for a declared header
or cookie, or the store raises. A token with nothing saved SHALL be refused exactly as a token lacking the
declared name. When a server declares `user_token`, the call SHALL NOT be sent, and SHALL return such an
error, in the refusals of the requirement above. Every error SHALL name the server and the reason, SHALL
name a missing header or cookie by its name only, and SHALL render no credential. No credential other than
the one the kind's rules select SHALL be substituted.

A server that is not connected SHALL report itself as not connected, not as an authorization failure.

#### Scenario: No recorded credentials means no request

- **WHEN** a browser-originated run whose token has nothing saved calls a tool of a `user_headers` server
- **THEN** no HTTP request carrying `tools/call` is sent
- **AND** the tool result is an error naming the server and a declared header or cookie the credentials
  lack

#### Scenario: A missing declared header means no request

- **WHEN** the parameters stored under the run's token lack one of the headers the server declares
- **THEN** no `tools/call` request is sent and the error names that header

#### Scenario: A store failure is a tool failure

- **WHEN** the security context store raises while a call resolves its credentials
- **THEN** the call returns a tool error and the task continues

#### Scenario: A server without user authorization is unaffected

- **WHEN** a run with no context calls a tool of a server whose `auth` kind is `none` or `mtls`
- **THEN** the call is sent as before and neither store is read

#### Scenario: A rejected credential does not end the shared session

- **WHEN** owner A's `tools/call` is answered with HTTP 401 while owner B's concurrent call on the same
  `streamable_http` connection is in flight
- **THEN** A's call returns a tool error, B's call completes, and the connection does not reconnect

#### Scenario: A refused resumption does not end the shared session

- **WHEN** owner A's `tools/call` response stream ends before its result, the resumption request carrying
  no credential is refused, and owner B's call on the same connection is in flight
- **THEN** A's call returns a tool error, B's call completes, and the connection does not reconnect

### Requirement: Per-call credentials never reach another origin

A forwarded header or cookie, and a `user_token` entry's `auth_header`, SHALL be present only on requests
whose scheme, host and port equal those of the server's configured URL. On a redirect to any other origin,
every name the connection forwards and the `Cookie` header SHALL be removed from the redirected request,
including names the HTTP client does not strip on its own.

#### Scenario: A cross-origin redirect drops the credentials

- **WHEN** a `tools/call` request carrying `X-Sudir-Token`, or a `user_token` entry's `X-API-Key`, is
  redirected to a different host
- **THEN** the request to that host carries neither header and no forwarded cookie

#### Scenario: A same-origin redirect keeps the forwarded header

- **WHEN** the redirect target has the same scheme, host and port as the configured URL
- **THEN** the redirected request carries the forwarded header
- **AND** carries no `Cookie`

### Requirement: Per-call credentials travel only over streamable HTTP

An entry declaring `user_headers` SHALL be refused when its `transport` is `sse`. An entry declaring
`user_token` over `sse` SHALL be refused unless it declares a static `auth_token`; such an entry SHALL send
that token as a header of the connection's HTTP client and SHALL never read the user token store. On `sse`
the SDK client sends no per-message request headers, and a rejected request ends the session every owner
shares.

#### Scenario: Forwarded headers over SSE are refused

- **WHEN** an entry declares `auth` of kind `user_headers` and `transport: sse`
- **THEN** construction fails naming the transport

#### Scenario: A personal token cannot ride SSE

- **WHEN** an entry declares `user_token` over `sse` without `auth_token`
- **THEN** construction fails stating that a personal token cannot ride `sse`

#### Scenario: A static token over SSE is a client header

- **WHEN** an entry declares `user_token` over `sse` with a static token, and a browser-originated run calls
  one of its tools
- **THEN** every request of the connection carries the static token
- **AND** the user token store is not read

### Requirement: Every authorization kind may address plain HTTP

An entry SHALL be accepted with an `http://` URL whatever its `auth` kind. The runtime SHALL NOT refuse a
`user_headers` or `user_token` entry for its scheme: carrying credentials over plain HTTP is the
responsibility of whoever declares the entry. An MCP server entry and an A2A remote-agent entry SHALL
follow the same rule.

`mtls` SHALL mean that the URL addresses the deployment's egress gateway, which originates mutual TLS to
the server with its own certificate. The runtime SHALL open an `mtls` connection exactly as a `none`
connection, SHALL add nothing to its requests and SHALL hold no client certificate. `mtls` SHALL be valid
over either transport.

#### Scenario: User credentials over plaintext are accepted

- **WHEN** an entry declares `user_headers` or `user_token` over `streamable_http` with an `http://` URL
- **THEN** it is accepted with that authorization

#### Scenario: A gateway-fronted server declares mtls over http

- **WHEN** an entry declares `auth` of kind `mtls` with an `http://` gateway URL, over `streamable_http` or
  over `sse`
- **THEN** it is accepted and its transport is opened with the same arguments as a `none` entry at that URL

#### Scenario: A plain http server declares no authorization

- **WHEN** an entry declares `auth` of kind `none` with an `http://` URL
- **THEN** it is accepted

#### Scenario: An A2A remote-agent entry follows the same rule

- **WHEN** an A2A remote-agent entry declares `none`, `mtls`, `user_headers` or `user_token` with an
  `http://` URL
- **THEN** it is accepted

