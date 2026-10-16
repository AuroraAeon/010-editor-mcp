# 010 Editor MCP Bridge

[English](README.md) | [简体中文](README.zh-CN.md)

A local **stdio MCP server** for binary-file analysis and an existing 010 Editor
session. The portable engine works on disk files; the editor bridge reads
in-memory buffers, including unsaved changes.

Requires Python 3.11+. The live backend has been tested on Windows with
010 Editor 16.0.4. [Verification results](docs/verification.md).

## Tools

| Tools | Behavior |
|---|---|
| `server_status`, `editor_status` | Backend availability, capabilities, limits and session diagnostics |
| `binary_inspect` | File metadata, identity and optional streamed hashes |
| `binary_read`, `binary_search`, `binary_diff` | Bounded byte reads, overlapping pattern matches and precise difference ranges |
| `binary_patch_preview`, `binary_patch_apply` | Check expected bytes, preview a replacement and write a new output copy |
| `editor_context`, `editor_read` | Active named buffer, dirty state, selection and unsaved bytes |
| `editor_select` | Select a byte range in the active document |
| `editor_run_template` | Run a trusted `.bt` and return XML plus parsed results |
| `editor_patch_preview` | Check live bytes and preview a replacement |
| `editor_patch_apply` | Returns `CAPABILITY_UNSUPPORTED` |

Editor patch application is disabled because 010 Scripts allow concurrent user
edits and provide no documented atomic expected-byte replacement. Previews return
`can_apply: false`. See the [mutation boundary](docs/architecture.md#editor-mutation-boundary).

## Install and run

Install Python 3.11+, [uv](https://docs.astral.sh/uv/), and
[pnpm](https://pnpm.io/installation), then:

```powershell
git clone https://github.com/AuroraAeon/010-editor-mcp.git
cd 010-editor-mcp
pnpm setup
pnpm start --allowed-root "C:\BinaryWork"
```

The server waits for an MCP client and reserves stdout for protocol messages.
Use `pnpm start --help` for configuration options.

For an installed wheel, run `uv pip install editor010_mcp-0.1.0-py3-none-any.whl`
in your Python environment. The wheel includes the bridge script. Node is needed
for repository commands through pnpm; the installed Python entry point runs on its own.

## MCP client configuration

After `pnpm setup`, configure your local MCP client with absolute paths:

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

Replace the sample paths with your installation paths. On Linux/macOS, use
`.venv/bin/editor010-mcp` for standalone tools. Live editor operations currently
require Windows. Omit the editor and template options for disk-only use.

## Connect 010 Editor

1. Start 010 Editor, dismiss startup dialogs and keep one GUI instance open.
2. Enable **Tools → Options → General → Allow Only One Instance of 010 Editor**.
3. Open the target file and add its directory as an `--allowed-root`.
   Named buffers may contain unsaved changes; untitled buffers need a filename.
4. Set `--editor-exe` and optionally `--editor-pid`. Call `editor_status`, then
   `editor_context`, and pass its `buffer_id` to subsequent editor tools.

The bridge forwards generated scripts with `-script:` to the existing session.
Operations address the active document. If its index, name or size changes,
request a fresh context after `STALE_CONTEXT`.

See [editor setup](docs/editor-setup.md) for configuration, limits and recovery.
The APIs require v13+; other editor versions remain unverified.

## Examples

> Inspect `C:\BinaryWork\sample.bin`, read 64 bytes at offset 32, search its first
> MiB for `50 4b 03 04`, and compare it with `sample.old.bin`.

> Read the selected bytes from the current 010 Editor buffer. Compare them with
> disk, then run the trusted ByteHeader template.

> Preview replacing `11 22` with `aa bb` at disk offset 1. Apply that preview to
> a new file, `sample.patched.bin`.

A disk patch requires two calls:

```json
{"tool":"binary_patch_preview","arguments":{"path":"C:\\BinaryWork\\sample.bin","offset":1,"expected_hex":"1122","replacement_hex":"aabb"}}
```

```json
{"tool":"binary_patch_apply","arguments":{"path":"C:\\BinaryWork\\sample.bin","offset":1,"expected_hex":"1122","replacement_hex":"aabb","output_path":"C:\\BinaryWork\\sample.patched.bin","preview_token":"TOKEN_FROM_PREVIEW"}}
```

The server rechecks the source identity and expected bytes while copying. It
rejects existing outputs, in-place writes, changed sources and expired or reused
tokens. Replacements must preserve byte length.

Tool offsets are **zero-based file-local byte offsets**. The editor's
`display_start_address` is separate. Raw template XML uses display addresses;
`variables[].offset` gives normalized local offsets. Results identify their data
source as `disk` or `editor`.

## Access and limits

`--allowed-root` defaults to the launch working directory and can be repeated.
Templates run only from locally configured trusted roots. Review their source
and includes before granting access: template execution uses 010 Editor's
permissions and may change Template Results or selection.

Reads, scans, copies and returned results have configurable bounds. See
[configuration defaults](docs/editor-setup.md#configuration) and
[architecture](docs/architecture.md) for path rules, concurrency and errors.

The bridge sends no file contents to remote services and does not log raw binary
data by default. Editor reads use the live buffer; an unavailable editor returns
an error instead of disk bytes.

## Development

```powershell
pnpm lint
pnpm test
pnpm exec uv build --wheel
```

The ordinary suite covers disk operations, transport contracts and real MCP
subprocess clients. Real-GUI tests are opt-in; use the
[live verifier command](docs/editor-setup.md#verify-the-real-gui).

[Documentation index](docs/README.md) · [Verification results](docs/verification.md)

MIT licensed. This independent integration is not affiliated with or endorsed
by SweetScape Software. 010 Editor is separately licensed.
