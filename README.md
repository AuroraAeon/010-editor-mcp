# 010 Editor MCP Bridge

A local **stdio MCP server** for bounded binary-file analysis and a user's existing
010 Editor GUI session. Python 3.11+, official MCP Python SDK 2.3.0. Independent
integration; not affiliated with or endorsed by SweetScape Software.

The standalone engine runs without 010 Editor. The live backend reads **in-memory,
unsaved bytes** through documented 010 Scripts; it never substitutes disk data.

## What works

| Tools | Behavior | Verification |
|---|---|---|
| `server_status`, `editor_status` | Limits, backend availability, session and feature diagnostics | Automated MCP client tests |
| `binary_inspect` | Size, identity, optional streamed hashes | Automated |
| `binary_read`, `binary_search`, `binary_diff` | Bounded raw bytes, overlapping pattern matches, exact difference ranges | Automated, including sparse large files |
| `binary_patch_preview`, `binary_patch_apply` | Expected-byte validation, expiring single-use preview, new output copy | Automated; original preserved |
| `editor_context`, `editor_read` | Active named buffer, dirty state, selection, unsaved bytes | Real Windows GUI, 010 Editor 16.0.4 |
| `editor_select` | Explicit zero-based byte selection in the intended active document | Real GUI |
| `editor_run_template` | Trusted `.bt`, XML export, parsed results with local offsets | Real GUI |
| `editor_patch_preview` | Checks live expected bytes; returns before/after without modifying them | Real GUI + automated |
| `editor_patch_apply` | Returns `CAPABILITY_UNSUPPORTED` | Deliberately disabled |

010 Scripts run concurrently with user edits. No documented atomic expected-byte
replacement or stable buffer-lifetime ID was found. Consequently this release
does **not** apply unsaved-buffer patches: previews return `can_apply: false`,
and apply returns an actionable typed error. This is a real, useful live
integration with an explicit mutation boundary. See the
[feasibility evidence](docs/bridge-feasibility.md).

## Install and run

Install Python 3.11+, [uv](https://docs.astral.sh/uv/), and
[pnpm](https://pnpm.io/installation), then:

```powershell
git clone https://github.com/AuroraAeon/010-editor-mcp.git
cd 010-editor-mcp
pnpm setup
pnpm test
pnpm start --allowed-root "C:\BinaryWork"
```

`pnpm` wraps repository commands; the delivered server is a Python program.
For a wheel installation, use `uv pip install editor010_mcp-0.1.0-py3-none-any.whl`
in a Python environment. The wheel includes the bridge script. No Node runtime
is needed to launch the installed Python entry point.

The server reserves stdout for MCP messages, so an interactive start waits
silently for a client. Use `pnpm start --help` for all configuration flags.

## MCP client configuration

After `pnpm setup`, point a local MCP client directly at the virtual environment:

```json
{
  "mcpServers": {
    "editor010": {
      "command": "C:\\Projects\\010-editor-mcp\\.venv\\Scripts\\editor010-mcp.exe",
      "args": [
        "--allowed-root", "C:\\BinaryWork",
        "--trusted-template-root", "C:\\Projects\\010-editor-mcp\\bridge\\templates",
        "--editor-exe", "C:\\Program Files\\010 Editor\\010Editor.exe"
      ]
    }
  }
}
```

Use absolute paths for your installation. On Linux/macOS the entry point is
`.venv/bin/editor010-mcp`; standalone tools work, while live tools report
`BACKEND_UNAVAILABLE`. Omit editor/template flags for standalone use. The MCP
tests cover current SDK discovery and legacy initialization.

## Connect the existing editor

1. Start 010 Editor manually and dismiss startup dialogs. Keep one GUI instance.
2. Enable **Tools → Options → General → Allow Only One Instance of 010 Editor**.
3. Open the intended ordinary file. Unsaved changes are supported; untitled
   buffers must first receive a filename. Add the document's directory as an
   `--allowed-root`.
4. Configure `--editor-exe` and optionally `--editor-pid` to pin the session.
5. Call `editor_status`, then `editor_context`. Use the returned `buffer_id`
   for read, selection, template, or preview operations.

The bridge forwards only its own generated, versioned scripts with `-script:`.
No plugin, native injection, or polling script is installed in the editor.
It detects the running process before dispatch and rejects ambiguous sessions.
A child-process environment guard rejects script execution in an accidental
new instance. Live operations target the **currently active** document and
reject changed index/name/size with `STALE_CONTEXT`; they never open or switch
to a source file to emulate a live read.

Verified on Windows with **010 Editor 16.0.4**. Used APIs require v13+;
other v13+ versions remain unverified. See [Windows setup and recovery](docs/editor-setup.md).

## Example workflows

Ask an agent:

> Inspect `C:\BinaryWork\sample.bin`, read 64 bytes at byte offset 32, and search
> the first MiB for `50 4b 03 04`. Compare it with `sample.old.bin`.

> Check the current 010 Editor buffer. Read its selected bytes from memory and
> compare them with the same disk offsets. Run our trusted ByteHeader template.

> Preview changing `11 22` to `aa bb` at byte offset 1 on disk, then apply that
> exact preview to a new file `sample.patched.bin`.

A disk patch is a two-call operation:

```json
{"tool":"binary_patch_preview","arguments":{"path":"C:\\BinaryWork\\sample.bin","offset":1,"expected_hex":"1122","replacement_hex":"aabb"}}
```

```json
{"tool":"binary_patch_apply","arguments":{"path":"C:\\BinaryWork\\sample.bin","offset":1,"expected_hex":"1122","replacement_hex":"aabb","output_path":"C:\\BinaryWork\\sample.patched.bin","preview_token":"TOKEN_FROM_PREVIEW"}}
```

The server checks source identity and expected bytes again during copying.
Existing outputs, in-place output, changed bytes, expired/replayed tokens, and
changed sources are rejected. Variable-length edits are outside this release.

All tool offsets are **zero-based file-local byte offsets**. The editor's
`display_start_address` is separate. Raw template XML uses display addresses;
`variables[].offset` is normalized to local bytes. `source` is always `disk`
or `editor`; a dirty editor buffer and its disk file are never equated.

## Limits and trust

The default allowed root is the launch working directory. Repeat `--allowed-root`
to grant additional directories. Canonical path checks reject escape through
symlinks, Windows devices, alternate data streams, and network paths. Ordinary
file operations assume the local filesystem and same-user processes are trusted;
this is not an isolation boundary against a hostile process changing directory
links or metadata concurrently.

| Setting | Default |
|---|---:|
| `--max-read-bytes`, `--max-patch-bytes` | 65,536 each |
| `--max-scan-bytes` | 268,435,456 |
| `--max-copy-bytes` | 1,073,741,824 |
| `--max-results` | 1,000 |
| `--chunk-size` | 1,048,576 |
| `--max-xml-bytes` | 1,048,576 |
| `--editor-timeout` | 15 seconds |
| `--preview-ttl-seconds` | 300 seconds |

Search/diff/hash refuse work beyond the scan limit; request a bounded range
or deliberately raise the limit. Diff byte previews have a separate aggregate
bound. Editor XML output is checked after export; the editor may generate a
larger temporary file before rejection. Trusted templates may perform expensive
work, and a timeout cannot forcibly cancel an already-running template.

Templates execute code with the permissions configured in 010 Editor. Trust
only reviewed templates and their includes; configured trust roots are an
operator decision, not a sandbox. Clients cannot grant new trust roots or run
arbitrary scripts. Source `FileSave` is never invoked by the bridge. Only owned
scratch response buffers are closed; source undo/history is untouched.

Requests use UUID correlation, per-process/thread serialization, a temporary
cancellation marker, and bounded waits. After a timeout/protocol failure, the
connection fails closed until the MCP server restarts. A queued request whose
marker has expired exits before acting. An already-running selection/template
has an **unknown outcome** after timeout; resolve the editor state before
restarting. Never retry a timed-out mutation blindly.

No remote transport is started, no file content is uploaded, and binary contents
are not logged by default. Temporary request/export files are removed on normal
completion and handled failure. Hard process termination may leave local
temporary files; see the cleanup instructions in the setup guide.

## Development and verification

```powershell
pnpm setup
pnpm lint
pnpm test
pnpm exec uv build --wheel
pnpm exec uv run --frozen python -m editor010_mcp.verify_live --help
```

The ordinary suite uses disposable fixtures and mocked bridge peers, plus real
MCP subprocess clients. Opt-in live tests use a real, already-running GUI and
are skipped by default. The live verifier's `--create-fixture` mode explicitly
opens an owned disposable file, creates an unsaved change, verifies the tools
through MCP, then closes only that fixture and restores the previous document.

See [verification results](docs/verification.md),
[architecture](docs/architecture.md), and [bridge evidence](docs/bridge-feasibility.md).
CI runs the portable suite and wheel-resource checks on Windows and Linux with
Python 3.11 and 3.13.

MIT licensed. 010 Editor is separately licensed software; no editor executable,
license data, or proprietary documentation is distributed here.
