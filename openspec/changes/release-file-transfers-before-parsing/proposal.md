## Why

Document parsing currently occupies file-transfer capacity after a download or before an upload. Other transfers are refused while the parser waits or processes a local file.

## What Changes

- Release download admission before attachment extraction and acquire upload admission after local staging and validation.
- Keep temporary files alive through parsing and preserve parser failure and cleanup behavior.
- Make Dialogs storage injection required, remove byte-based media delivery and duplicate image suffixes, and move inbound persistence helpers into their owning class.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `attachment-parsing`: parsing local files does not consume file-transfer admission.

## Impact

Attachment storage and tools, Dialogs composition and media delivery, inbound persistence, and their focused tests. No database migration, new dependency, or public wire-format change.
