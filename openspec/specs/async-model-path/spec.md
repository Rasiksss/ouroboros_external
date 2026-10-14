# async-model-path Specification

## Purpose
TBD - created by archiving change add-an-asynchronous-model-path. Update Purpose after archive.
## Requirements
### Requirement: The asynchronous path is additive and shares the synchronous path's decisions

Model routing SHALL offer an asynchronous entry point alongside the synchronous one. Adding it SHALL
NOT change the synchronous entry point's behaviour, signature or call sites.

The two paths SHALL share every decision that is not I/O: which targets a model resolves to and in
what order, the provider circuit-breaker state, the response validation that decides whether an
answer is usable, the token accounting, and the fallback rule. Those SHALL live in one place that
both paths call. Only the transport and the waiting SHALL differ.

Sharing SHALL be by extraction rather than by reimplementation. A second copy of the fallback rule is
a second answer to "which model answered", discovered when the two disagree in production.

#### Scenario: The synchronous path is unchanged

- **WHEN** the asynchronous path is added
- **THEN** the synchronous entry point keeps its signature and its behaviour
- **AND** no existing caller of it changes

#### Scenario: One circuit-breaker state serves both paths

- **WHEN** the asynchronous path records a provider failure and the synchronous path then resolves a
  target for the same provider
- **THEN** it observes that failure
- **AND** neither path keeps circuit state of its own

#### Scenario: The fallback rule is written once

- **WHEN** the two paths are inspected
- **THEN** the rule that selects one distinct fallback, shares the deadline with it and accumulates
  token figures is declared once and called by both

### Requirement: The asynchronous path carries tool calls

The asynchronous entry point SHALL accept tool schemas and SHALL return tool calls the model made, on
the same terms as the synchronous entry point. An asynchronous path that refuses tools is not a path
the callers that need it can take: every background-memory handler that calls a model either passes
tools or shares a code path with one that does.

The refusal the existing unused asynchronous method carries SHALL NOT be preserved. That method SHALL
be removed rather than extended, because keeping an unreachable second entry point beside a real one
is how a caller later takes the wrong one.

#### Scenario: A tool-carrying request succeeds on the asynchronous path

- **WHEN** an asynchronous call is made with tool schemas and the model answers with a tool call
- **THEN** the call returns that tool call
- **AND** it does not raise that tool calls are unsupported

#### Scenario: The unreachable asynchronous method is gone

- **WHEN** the model client is inspected
- **THEN** it declares no asynchronous method that refuses tool calls

### Requirement: An asynchronous transport uses asynchronous hooks

Where the synchronous path installs a callable on its HTTP client — a tracing hook, a header
collector, a retry observer — the asynchronous path SHALL install the asynchronous form of it. An
asynchronous HTTP client awaits its hooks, so a synchronous callable installed on it fails on the
first request rather than degrading, and it fails inside the transport where the error names the hook
and not the caller.

Every such hook SHALL be identified before the path is built, and each SHALL have an asynchronous
counterpart or be deliberately absent from the asynchronous path.

#### Scenario: A request through the asynchronous transport carries its trace

- **WHEN** an asynchronous model call is made while an operation is active
- **THEN** the outbound request carries the operation's trace header
- **AND** the call does not fail inside the transport

#### Scenario: Every hook is accounted for

- **WHEN** the hooks the synchronous transport installs are compared with the asynchronous one's
- **THEN** each is present in its asynchronous form or explicitly declared absent

### Requirement: Waiting for a model-call slot does not occupy the caller's event loop

An asynchronous caller SHALL obtain its model-call slot without blocking the thread it runs on. The
wait SHALL yield, so that other work on the same loop proceeds while a call waits for admission.

The slot SHALL still be taken: the asynchronous path SHALL NOT bypass the budget. A path that skips
admission has no ceiling at all, which is a different failure from a ceiling that is too low.

The holder identity a lease carries SHALL identify the work, not the operating-system thread. Under a
loop many concurrent calls share one thread, so a thread identifier names every one of them the same
and the budget can no longer tell two holders apart.

#### Scenario: A waiting call does not stall its neighbours

- **WHEN** every model-call slot is held and another asynchronous call asks for one
- **THEN** other work on the same event loop continues to run while it waits

#### Scenario: The asynchronous path still takes a slot

- **WHEN** an asynchronous model call is made
- **THEN** it holds a slot from the model-call budget for the duration of the call
- **AND** releases it on every exit path, including a failure and a cancellation

#### Scenario: Two concurrent calls on one thread are two holders

- **WHEN** two asynchronous model calls run concurrently on one event loop
- **THEN** the budget records two distinct holders

