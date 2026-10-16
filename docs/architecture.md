# Architecture

010 Editor MCP Bridge is one local stdio MCP server with two data sources. The disk engine operates without 010 Editor; the editor backend operates on the existing GUI session. Results identify their source as `disk` or `editor`. A live editor failure never turns into a filesystem read.

## Components

| Component | Responsibility |
| --- | --- |
| `server.py` | Official MCP SDK registration, input schemas, stdio lifecycle, and sanitized typed tool errors. |
| `models.py` | Configuration, byte-range and hex validation, path boundaries, and shared error types. |
| `standalone.py` | Bounded disk inspection, reads, search, comparison, patch preview, and copy-on-write patching. |
| `editor_backend.py` | Active-buffer context, identity checks, live reads, selection, trusted templates, and patch previews. |
| `bridge_protocol.py` | Serialized request execution, Windows process checks, generated scripts, correlated replies, and timeouts. |
| `verify_live.py` | An opt-in experiment using disposable files and a real existing editor process. |

The MCP layer calls backend methods; the portable engine does not import Windows automation or depend on a running GUI. The transport owns script generation. Callers cannot submit arbitrary `.1sc` source through an MCP tool.

## Disk operations

Offsets count bytes from the beginning of an ordinary file. Reads seek to the requested interval; scans and copies stream chunks. Configured limits bound input intervals, returned data, matches, difference ranges, patch size, and copied bytes. A bounded comparison does not imply that unexamined regions match.

A disk preview checks `expected_hex`, records source identity, and returns a short-lived token. Apply requires the same path, offset, expected bytes, replacement bytes, and token, plus a new output path. It rechecks the source while streaming the copy. Existing outputs and source-path outputs are rejected. There is no in-place overwrite mode in this release.

Path policy resolves paths against locally configured allowed roots and rejects special device, process, network, and alternate-stream targets. This bounds operations by ordinary filesystem permissions; it does not provide a sandbox against a hostile process running as the same operating-system user.

## Existing-session bridge

The selected transport forwards `-script:` requests through the installed 010 Editor executable to its existing single GUI instance. The official manual documents command-line script execution and the **Allow Only One Instance of 010 Editor** setting. The observed forwarding behavior and installed version are recorded separately in [bridge-feasibility.md](bridge-feasibility.md). [Command line](https://www.sweetscape.com/010editor/manual/CommandLine.htm), [General Options](https://www.sweetscape.com/010editor/manual/OptionsGeneral.htm).

Before dispatch, the bridge checks the process executable, PID, and process start time. It rejects an absent editor, an unexpected process, or multiple editor instances. It does not start a new GUI, pass a source filename to open, save a source document, or switch to another document to satisfy a request.

Each serialized request has a UUID, a private temporary directory, a generated fixed-operation script, and distinct response/payload paths. The script checks request validity before an operation with visible side effects. Replies must match the pending request. Output payload sizes are checked before they enter an MCP result. Temporary files hold local binary data only for the request lifetime and are cleaned up by the bridge.

Timeout means the operation's outcome is unknown. The transport becomes unusable until a fresh connection is established after the editor's busy script or dialog is resolved. Expired requests must not execute a delayed selection or template operation. A successful forwarding subprocess exit is not a successful editor response.

## Buffer identity and consistency

`editor_context` returns an opaque buffer token tied to the server's context snapshot, editor process lifetime, current file index, and filename. Subsequent tools require that token and validate the intended active document. A changed target produces `STALE_CONTEXT`; the backend does not silently select an old target.

010 Editor documents `GetFileNum()` as an index, not a lifetime identifier. The current API does not expose a documented persistent buffer UUID. Closing and reopening the same path can therefore recreate an indistinguishable index/name pair. Tokens are context checks, not a guarantee that a tab has never been recreated. Live context and read results describe observations, not a multi-operation snapshot. [Interface functions](https://www.sweetscape.com/010editor/manual/FuncInterface.htm).

Read operations export bytes from the editor buffer to a temporary payload with `FileSaveRange`. The source document is not saved. Responses are written through an owned temporary document; only that temporary document is closed. The bridge reports `GetStartingAddress()` separately from local zero-based offsets, and rejects drive and process buffers.

## Template execution

Only `.bt` files under locally configured trusted template roots may run. Roots are startup configuration, not remotely editable MCP state. A trusted template is executable local code: root membership is an authorization decision, not a sandbox. Keep trusted roots outside locations where untrusted clients can replace templates.

The bridge runs the template on the validated active document, expands results, and exports bounded XML. This changes the document's associated Template Results. Suppressed or lazy nodes can limit exported detail. Missing templates and template errors can abort their calling script; a timeout does not prove that the template never started. 010 Editor's own permission settings also apply. [Interface functions](https://www.sweetscape.com/010editor/manual/FuncInterface.htm), [Template permissions](https://www.sweetscape.com/010editor/manual/OptionsPermissions.htm).

## Editor mutation boundary

`editor_patch_preview` compares expected bytes against the live buffer and describes the proposed replacement. `editor_patch_apply` returns `CAPABILITY_UNSUPPORTED` in this release.

Scripts run in a separate thread and users can edit while they run. The documented API offers no compare-and-swap or exclusive editing transaction. A script that reads, compares, and writes bytes can overwrite an intervening edit. Preview tokens and a second byte comparison do not eliminate this race, and `SetReadOnly` is not a documented transaction lock. Enabling editor mutation requires evidence of an atomic validation-and-write mechanism that preserves undo and leaves the source unsaved. [Running Templates and Scripts](https://www.sweetscape.com/010editor/manual/Running.htm).

## Errors and evidence

Expected failures are machine-readable MCP tool errors, including unavailable backends, unsupported capabilities, stale context, invalid arguments, bounds and limit violations, path denial, expected-byte conflicts, timeout, and I/O failure. Logs do not include raw binary bytes by default. The bridge itself sends no file contents to remote services; the connected MCP client controls how returned tool data is used.

Automated tests exercise deterministic disk behavior and transport contracts. A mocked reply proves contract handling only. Real GUI capability claims require the disposable live verifier and the evidence log; see [editor-setup.md](editor-setup.md).
