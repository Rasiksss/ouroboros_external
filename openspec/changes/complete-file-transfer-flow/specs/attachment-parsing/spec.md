## ADDED Requirements

### Requirement: Stored-file send validation releases transfer admission
Sending an unvalidated stored document SHALL release its download lease before waiting for or running content validation. The local file SHALL survive through validation and be removed on success or failure.

#### Scenario: Another replica transfers during send validation
- **WHEN** send_file validates a downloaded document
- **THEN** another replica can acquire otherwise available owner transfer capacity
- **AND** a malformed document still fails validation without delivery

### Requirement: Refused transfer admission reserves no uploaded file
An upload refused for transfer capacity SHALL reserve neither a catalog record nor a filename. Content validation SHALL still happen before transfer admission.

#### Scenario: Retrying a capacity refusal preserves the filename
- **WHEN** a file upload is refused for owner or process transfer capacity and retried after capacity becomes available
- **THEN** the refused attempt leaves no file reservation that renames the successful upload
