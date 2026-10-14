## ADDED Requirements

### Requirement: Local parsing does not hold file-transfer admission
Attachment extraction SHALL release its download lease before waiting for or running the parser. Synchronous uploads SHALL stage and validate local content before acquiring upload admission. Temporary files SHALL remain available until parsing or validation finishes and SHALL be cleaned up on success and failure. The S3 transfer itself SHALL remain subject to shared owner and process limits.

#### Scenario: Another replica transfers while a document is parsed
- **WHEN** a downloaded document is waiting for or running its parser
- **THEN** another replica can acquire the owner's otherwise available transfer capacity
- **AND** extraction retains access to the downloaded temporary file

#### Scenario: An upload validates before admission
- **WHEN** a synchronous upload validates its staged document
- **THEN** validation occupies no file-transfer lease
- **AND** the subsequent S3 upload acquires admission before writing content

#### Scenario: Invalid content is rejected
- **WHEN** extraction or upload validation rejects a document
- **THEN** the existing attachment error is preserved
- **AND** the temporary file is removed and no transfer lease remains
