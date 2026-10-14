# attachment-parsing Specification

## Purpose
TBD - created by archiving change lightweight-attachment-parser. Update Purpose after archive.
## Requirements
### Requirement: Document parsing starts without application composition imports
The runtime SHALL parse each document in a fresh spawned process. Importing the parser target SHALL NOT import application configuration, storage, LLM clients, tool composition or server entrypoints. The parent SHALL pass the path, MIME, operation and CPU budget explicitly.

#### Scenario: A text upload starts its worker
- **WHEN** browser or Dialogs validates a text document
- **THEN** the child imports the minimal parser module without importing the tool facade or application services
- **AND** the existing text validation and decoding behavior is preserved

### Requirement: Parser isolation and error behavior survive worker extraction
The parser SHALL retain its current address-space and CPU limits, bounded extracted output, admission timeout, wall-clock timeout and process cleanup. Unsupported or invalid documents SHALL preserve their public errors, and temporary parser unavailability SHALL remain retryable.

#### Scenario: A parser exceeds its wall-clock budget
- **WHEN** the parser fails to finish within its configured deadline
- **THEN** the parent terminates and reaps the child before releasing its parser slot
- **AND** the caller receives retryable parser unavailability

#### Scenario: Browser validation is cancelled
- **WHEN** a browser validation is cancelled with a parser worker in flight
- **THEN** the parent waits for bounded worker cleanup before releasing the slot and reporting cancellation

### Requirement: Parser phases are observable without document content
The parent SHALL record operation, MIME, outcome and durations for parser admission and execution. Successful worker responses SHALL distinguish startup, optional-library import, parsing, result transfer and cleanup. Missing phase measurements SHALL remain distinguishable from zero. Timing logs SHALL NOT include document contents, filenames or paths. Browser uploads SHALL distinguish copying, validation and storage durations.

#### Scenario: A document parses successfully
- **WHEN** a spawned worker returns its result
- **THEN** the parent records separate measured parser phases
- **AND** the returned attachment payload is unchanged

#### Scenario: No worker result is available
- **WHEN** admission times out or a child exits without returning a result
- **THEN** diagnostics identify the failure and elapsed phase without fabricating child measurements

