# Architecture

010 Editor MCP Bridge is one local stdio MCP server with two data sources. The disk engine operates without 010 Editor; the editor backend operates on the existing GUI session. Results identify their source as `disk` or `editor`. A live editor failure never turns into a filesystem read.

## Components

| Component | Responsibility |
| --- | --- |
| `server.py` | Official MCP SDK registration, input schemas, stdio lifecycle, and sanitized typed tool errors. |
| `models.py` | Configuration, byte-range and hex validation, path boundaries, and shared error types. |
| `standalone.py` | Bounded disk inspection, reads, search, comparison, patch preview, and copy-on-write patching. |
| `editor_backend.py` | Active-buffer context, identity checks, live reads, selection, trusted templates, and patch previews. |
| `bridge_protocol.py` | Serialized requests, generated scripts, correlated replies and timeouts. |
| `windows.py` | Windows process identity, GUI availability and installed-version discovery. |
| `verify_live.py` | An opt-in experiment using disposable files and a real existing editor process. |

The MCP layer calls backend methods. The portable engine has no Windows dependency. The transport renders fixed-operation scripts; MCP clients cannot submit `.1sc` source.

## Disk operations

Offsets count bytes from the beginning of an ordinary file. Reads seek to the requested interval; scans and copies stream chunks. Limits bound input ranges, returned data, matches, patch size and copied bytes. A bounded comparison covers only the requested range.

A disk preview checks `expected_hex`, records source identity, and returns a short-lived token. Apply requires the same path, offset, expected bytes, replacement bytes, and token, plus a new output path. It rechecks the source while streaming the copy. Existing outputs and source-path outputs are rejected. There is no in-place overwrite mode in this release.

Path policy resolves targets against configured allowed roots. It rejects devices, live processes, network paths and alternate data streams. The policy assumes a trusted local filesystem; a same-user process can still race path or metadata changes.

## Existing-session bridge

The transport forwards `-script:` requests through the installed executable to one existing GUI session. It requires **Allow Only One Instance of 010 Editor**. [Command line](https://www.sweetscape.com/010editor/manual/CommandLine.htm), [General Options](https://www.sweetscape.com/010editor/manual/OptionsGeneral.htm).

Before dispatch, the bridge checks the executable, PID and process creation time. It rejects missing, unexpected or multiple sessions. Live tools address the active document without opening source files. A process environment guard prevents script execution in an accidentally launched new instance.

Each request has a UUID, a private temporary directory, a cancellation marker and separate response/payload paths. Thread and process locks serialize requests. The script checks its marker before side effects, then publishes its response after closing the owned scratch buffer and restoring its I/O target. The server validates the reply ID and payload size before returning data.

After a timeout or protocol error, the transport rejects further requests until the MCP server restarts. Removing the marker cancels queued work; a selection or template that already started has an unknown outcome. Resolve the editor state before reconnecting.

## Buffer identity and consistency

`editor_context` returns an expiring token tied to process lifetime, active file index, filename and size. Subsequent tools validate that identity before dispatch and again in the response. A mismatch returns `STALE_CONTEXT`.

The API exposes a file index but no documented persistent buffer UUID. Closing and reopening the same path with the same size may reproduce that identity. Concurrent edits can also change observations between calls. [Interface functions](https://www.sweetscape.com/010editor/manual/FuncInterface.htm).

Read operations export bytes from the editor buffer to a temporary payload with `FileSaveRange`. The source document is not saved. Responses are written through an owned temporary document; only that temporary document is closed. The bridge reports `GetStartingAddress()` separately from local zero-based offsets, and rejects drive and process buffers.

## Template execution

Only `.bt` files under locally configured trusted roots may run. Templates and their includes are executable local code; keep reviewed copies in directories untrusted clients cannot overwrite. Root configuration is fixed at server startup.

The bridge runs the template, expands results and exports XML. This changes Template Results and may change selection. Suppressed or lazy nodes can be omitted. XML limits are checked after export, so the editor may first create a larger temporary file. A template failure can abort its calling script; the server then times out. [Interface functions](https://www.sweetscape.com/010editor/manual/FuncInterface.htm), [Template permissions](https://www.sweetscape.com/010editor/manual/OptionsPermissions.htm).

Raw XML uses editor display addresses. Parsed `variables[].offset` values subtract `display_start_address` to give zero-based file-local offsets. The template hash is checked before and after execution.

## Editor mutation boundary

`editor_patch_preview` compares expected bytes against the live buffer and describes the proposed replacement. `editor_patch_apply` returns `CAPABILITY_UNSUPPORTED` in this release.

Scripts run in a separate thread while users can edit. The documented API provides no atomic compare-and-write or exclusive editing transaction; `SetReadOnly` has no such guarantee. Editor apply therefore stays disabled until a mechanism can validate and write atomically while preserving undo and unsaved state. [Running Templates and Scripts](https://www.sweetscape.com/010editor/manual/Running.htm).

## Errors and evidence

Expected failures are machine-readable MCP tool errors, including unavailable backends, unsupported capabilities, stale context, invalid arguments, bounds and limit violations, path denial, expected-byte conflicts, timeout, and I/O failure. Logs do not include raw binary bytes by default. The bridge itself sends no file contents to remote services; the connected MCP client controls how returned tool data is used.

Run instructions are in [editor setup](editor-setup.md); current outcomes are in [verification results](verification.md). The [feasibility log](bridge-feasibility.md) records the dated bridge experiments.
