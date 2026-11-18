---
name: workstation
description: Execute code, read/write/edit files in an isolated AEF WorkStation sandbox, and return results as context or files.
version: 1.0.0
type: instruction
when_to_use: The user asks to run code, execute commands, create or edit files in a sandbox, test generated code, or work with files in an isolated environment; the task requires shell access, file manipulation, or code execution that benefits from a sandboxed workspace. Russian equivalents include выполни код, запусти в песочнице, создай файл, выполни команду, проверь код.
---

# WorkStation

WorkStation provides an isolated sandbox session for code execution and file
operations.  One session is shared across all tasks in the same conversation:
files and state persist between turns.

## Step 1: connect

**Before using any file or exec tool, call `workstation_connect`.**

```
workstation_connect()
```

This creates or resumes the sandbox session for this conversation.  It is
a no-op if already connected this turn.  If it returns `TOOL_UNAVAILABLE`
or `WORKSTATION_UNAVAILABLE`, the sandbox is not reachable — tell the user
and continue with local tools if possible.

## Available tools

### workstation_connect

Create or resume a WorkStation session.  Must be called first.

### code_exec

Run a shell command inside the sandbox.

```
code_exec(command, workdir?, env?, timeout?)
```

- `command` (required): shell command string.
- `workdir`: working directory (defaults to `./`).
- `env`: extra environment variables.
- `timeout`: seconds (service caps at 300).

Returns stdout, stderr, and exit_code.  Use this to install dependencies,
run scripts, compile code, or execute any shell command.

### file_read

Read a file from the sandbox and deliver it to the user.

```
file_read(path)
```

- `path` (required): virtual path inside the sandbox.

After reading, the file is delivered to the user by its type: images via
`send_image`, everything else via `send_file`.
Directories return a listing.

### file_write

Write content to a file in the sandbox.

```
file_write(path, content?, data_path?)
```

- `path` (required): virtual path inside the sandbox.
- `content`: text content to write (default).
- `data_path`: a data-drive path whose raw bytes are copied into the
  sandbox; used to place a user-uploaded file there. For a user upload,
  pass `uploads/chat/<id>` (the attachment id shown in the task
  manifest), optionally `uploads/chat/<id>/<name>`, or `uploads/<name>`
  (resolved by filename). Any `uploads/<file>` not found as a stored
  upload is read from the local data drive.

`content` and `data_path` are mutually exclusive. `data_path` must live
under `uploads/`. Creates the file and parent directories if needed.
Overwrites existing files.  Use for new files or complete rewrites.

### file_edit

Apply targeted search-and-replace edits to a file.

```
file_edit(path, edits[{oldText, newText}])
```

Each `oldText` must match exactly one unique region in the original file.
Multiple edits in one call are matched against the original, not
incrementally.  Keep `oldText` as small as possible while remaining unique.

### apply_patch

Apply a multi-file patch (add, delete, update, rename).

```
apply_patch(input)
```

Format: `*** Begin Patch` ... `*** End Patch` with sections per file.
All hunks are validated before any file is written — if any hunk fails,
no file changes.

## Session lifecycle

- Call `workstation_connect` once per conversation turn that needs the
  sandbox.  Repeated calls are no-ops.
- The session is scoped to the **conversation** (`chat_id`), not to an
  individual task.  Every task in the same chat reuses the same sandbox.
- Files written in one turn are visible in subsequent turns.
- The session is automatically kept alive while tools are being called.

## Workflow

Typical pattern for a code-generation-then-execution task:

1. `workstation_connect` — create or resume the sandbox session.
2. `file_write` — save the generated code into the sandbox.
3. `code_exec` — run it, capture stdout/stderr.
4. If the output is the answer, return it as context.
5. If the user needs a file, `file_read` the result — it is delivered via
   `send_image`/`send_file` by its type.

To work on a file the user uploaded: copy it into the sandbox with
`file_write(data_path='uploads/chat/<id>')` using the attachment id from
the task manifest (or `data_path='uploads/<name>'`), process it with
`code_exec`, and return the result with `file_read`.

For iterative work (test → fix → retest), the session persists across
turns, so you can `file_edit` the code and `code_exec` again without
re-uploading.

## Error handling

| Signal | Meaning | Action |
|---|---|---|
| `WORKSTATION_UNAVAILABLE` | Integration not configured | Tell the user sandbox is not available |
| `TOOL_UNAVAILABLE` | No session bound to this turn | Call `workstation_connect` first |
| `ACCESS_DENIED` | Workspace is read-only (`access_level != "rw"`) | Use read-only tools or inform the user |
| `WORKSTATION_ERROR` | Transport or service error | Retry once; if it persists, report to user |
| `timeout` | Command exceeded time limit | Split into smaller steps or increase timeout |
| exit_code != 0 | Command failed | Read stderr, fix, and retry |

## Constraints

- Max exec timeout: 300 seconds (service-side limit).
- Files live inside `./` of the sandbox session.
- The sandbox is ephemeral: it does not survive process restarts when
  using the default `ephemeral` session strategy.
