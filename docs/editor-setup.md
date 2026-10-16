# Existing 010 Editor session setup

This integration is independent of SweetScape. Use [bridge-feasibility.md](bridge-feasibility.md) for the installed-version evidence and the exact capabilities that have passed real GUI validation. The initial validation target is Windows 11 with 010 Editor 16.0.4. Other versions remain unverified; the script functions require at least v13 because the bridge reports the display starting address.

## Prepare the local environment

From the repository, install the Python project and development dependencies:

```powershell
pnpm setup
```

Start 010 Editor yourself. In **Tools > Options > General > Startup**, enable **Allow Only One Instance of 010 Editor**. Close extra editor instances after preserving their documents. The bridge requires one existing process and will not launch another one. The setting is documented in [General Options](https://www.sweetscape.com/010editor/manual/OptionsGeneral.htm); forwarding and unsaved-buffer behavior still require the live check below.

Use PowerShell to identify the intended process:

```powershell
Get-Process -Name 010Editor | Select-Object Id, Path, StartTime
```

Configure its executable path and PID. The following paths and PID are examples; replace them with your local values:

```powershell
pnpm start --allowed-root C:\BinaryWork --editor-exe 'C:\Program Files\SweetScape\010 Editor\010Editor.exe' --editor-pid 1234 --trusted-template-root C:\BinaryTemplates
```

This starts an MCP stdio server, so it waits for a client and reserves stdout for protocol messages. A regular shell prompt is not an MCP client. To use it from your coding agent, put the same executable and arguments in that client's MCP server configuration as described in the [README](../README.md).

`--allowed-root` limits ordinary disk targets and addressed named editor buffers. `--trusted-template-root` authorizes template execution from that directory and can be repeated. No templates are trusted by default. Choose a dedicated trusted directory whose contents you have reviewed and that untrusted clients cannot overwrite. Template includes and 010 Editor permissions remain part of that code's trust boundary. [Template permissions](https://www.sweetscape.com/010editor/manual/OptionsPermissions.htm).

## Configuration

CLI flags override corresponding environment variables. Multiple roots use the platform's path-list separator: `;` on Windows and `:` on POSIX. Use absolute paths for client configurations so their working directory cannot change which files are authorized.

| CLI flag | Environment variable | Purpose/default |
| --- | --- | --- |
| `--allowed-root` | `EDITOR010_ALLOWED_ROOTS` | Disk roots; defaults to the server's working directory. |
| `--trusted-template-root` | `EDITOR010_TRUSTED_TEMPLATE_ROOTS` | Authorized `.bt` roots; empty by default. |
| `--editor-exe` | `EDITOR010_EDITOR_EXE` | Installed executable used for existing-instance forwarding. |
| `--editor-pid` | `EDITOR010_EDITOR_PID` | Pin the intended existing process. |
| `--bridge-dir` | `EDITOR010_BRIDGE_DIR` | Optional local directory for private request files. |
| `--editor-timeout` | `EDITOR010_EDITOR_TIMEOUT` | Reply timeout; 15 seconds. |
| `--preview-ttl-seconds` | `EDITOR010_PREVIEW_TTL_SECONDS` | Preview token lifetime; 300 seconds. |
| `--max-read-bytes` | `EDITOR010_MAX_READ_BYTES` | Returned byte interval; 65,536 bytes. |
| `--max-xml-bytes` | `EDITOR010_MAX_XML_BYTES` | Template XML output; 1,048,576 bytes. |

Additional bounded disk-work limits are available through `pnpm start --help`. Keep bridge files on a private local filesystem. Request files and payloads can contain source bytes; avoid shared or synchronized directories.

## Verify the real GUI

The live verifier is opt-in because it opens and changes a disposable test document in the existing GUI. Preserve your current work first. It must use its own fixture and restore the original active document; it must never save or close your documents.

Inspect its options using the project environment:

```powershell
pnpm exec uv run --frozen python -m editor010_mcp.verify_live --help
```

Run it explicitly against an already-running editor (replace the example path/PID):

```powershell
pnpm exec uv run --frozen python -m editor010_mcp.verify_live --create-fixture --editor-exe 'C:\Program Files\010 Editor\010Editor.exe' --editor-pid 1234 --work-dir .local/live
```

The final full verifier run remains pending after the last corrections; see [passed, failed, and not-run evidence](verification.md). Record the result alongside the installed editor version. The acceptance sequence is:

1. Record the existing process lifetime and original active document.
2. Create an ordinary disposable fixture, open only that fixture, and record its disk bytes.
3. Change a known fixture byte in the editor without saving. Confirm dirty state.
4. Call the bridge context/read path and verify that it returns the changed byte while the source file still contains the original byte.
5. Select a fixture byte range and confirm the returned selection.
6. Run a reviewed fixture `.bt` and validate its exported XML.
7. Verify stale-context and expected-byte conflict handling, then close only the owned fixture and restore the user's original active document.

The verifier's executed results and [bridge-feasibility.md](bridge-feasibility.md) determine what is verified. A test skip, a mock transport, or an executable returning exit code zero does not establish live integration.

## Use the live tools

Begin with `server_status` and `editor_status`. Keep the intended ordinary document active, obtain its `buffer_id` from `editor_context`, and pass that value to `editor_read`, `editor_select`, `editor_run_template`, or `editor_patch_preview`. If you switch documents, obtain a fresh context. All byte offsets are zero-based local offsets; a custom hex display address does not change the tool arguments.

`editor_read` returns the editor's current data, including unsaved edits. It never substitutes source-file bytes. `editor_select` changes selection. Template execution changes Template Results and may expose a permission dialog if the trusted template requests additional access. Context and read tools preserve the source bytes and selection.

Editor patch preview is available. Editor patch apply returns `CAPABILITY_UNSUPPORTED`: the documented scripting API permits concurrent GUI editing and does not provide atomic expected-byte validation plus write. To make a reviewed patch today, use the disk backend's new-output workflow and review that output, or perform the edit manually in 010 Editor. The architecture explains the [mutation boundary](architecture.md#editor-mutation-boundary).

## Recover from failures

| Result | Action |
| --- | --- |
| `BACKEND_UNAVAILABLE` | Open the intended GUI yourself; verify its executable/PID, one-instance setting, and version. Disk tools remain usable. |
| `STALE_CONTEXT` | Activate the intended document, call `editor_context`, and review the new identity before retrying. |
| Expected-byte conflict | Re-read the relevant bytes and review the proposed replacement. |
| Timeout or editor busy | Resolve the editor's running script or dialog, then restart/reconnect the MCP server. The timed-out operation's outcome is unknown. |
| Template failure | Check 010 Editor's Output panel and template permissions; resolve the failure before reconnecting after a timeout. |
| `LIMIT_EXCEEDED` | Request a smaller interval/output or explicitly change the local limit. |
| `CAPABILITY_UNSUPPORTED` for patch apply | No retry will enable atomic patching in this release. |

Do not retry timed-out side effects blindly. Request expiration guards prevent queued old work from being treated as a new request, but a timeout cannot undo an operation that already began. The server never terminates the editor to recover.

## Temporary-file cleanup

The default request directory is `%LOCALAPPDATA%\editor010-mcp` on Windows. Successful requests and handled failures remove their request directories; process lock files contain no binary data and may remain. A hard crash can leave `request-*` directories with temporary source bytes. After the bridge server is stopped and the editor has finished its scripts, inspect that directory and remove only the identified stale request directories. Do not remove an active request directory.

Verifier artifacts live under the explicit `--work-dir`. On cleanup failure they remain for inspection and the JSON report says `artifacts_retained: true`. Confirm their owned `probe.bin` is closed before deleting the corresponding `verification-*` directory. Never close or delete another document to make a verifier pass.
