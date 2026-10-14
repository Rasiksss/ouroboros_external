## Context

The parser has its own admission and worker cleanup. Shared PostgreSQL transfer leases currently cover local parsing in two synchronous flows.

## Goals / Non-Goals

**Goals:** Scope transfer leases to storage work and complete the requested Dialogs and inbound persistence simplifications.

**Non-Goals:** Change parser limits, HTTP upload recovery, migration cutover, broker behavior, or database schema.

## Decisions

- Keep temporary-directory lifetime outside the transfer context. Extraction releases admission after download; uploads acquire it after staging and validation, before catalog reservation and S3 publication.
- Retain existing parser admission rather than adding another queue or limiter.
- Inject the composed storage provider through the Dialogs factory and constructor. Media delivery accepts only the production `StoredFile` representation and shares the format-policy suffix map.
- Keep inbound persistence in its current owner and move its private helpers into `InboundMessageStore` without changing transaction behavior.

## Risks / Trade-offs

- Local upload staging no longer occupies an S3 slot; parser admission and existing caller bounds remain responsible for local processing. Transfer limits still apply before reservation and S3 writes.
- Constructor and media signatures change internally; update all production roots and test callers together.
