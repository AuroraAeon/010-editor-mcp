# Existing 010 Editor session setup

The live backend supports an existing Windows GUI session. Verification covers 010 Editor 16.0.4; the APIs require v13+. See [verification results](verification.md) for current outcomes.

## Prepare the local environment

From the repository, install the Python project and development dependencies:

```powershell
pnpm setup
```

Start 010 Editor and enable **Tools > Options > General > Allow Only One Instance of 010 Editor**. Save your work before closing extra instances. Dismiss startup dialogs, open the intended file, and keep its tab active. [General Options](https://www.sweetscape.com/010editor/manual/OptionsGeneral.htm).

Use PowerShell to identify the intended process:

```powershell
Get-Process -Name 010Editor | Select-Object Id, Path, StartTime
```

Configure its executable path and PID. The following paths and PID are examples; replace them with your local values:

```powershell
pnpm start --allowed-root C:\BinaryWork --editor-exe 'C:\Program Files\010 Editor\010Editor.exe' --editor-pid 1234 --trusted-template-root C:\BinaryTemplates
```

The stdio server waits for a client and reserves stdout for protocol messages. Put its executable and arguments in your [MCP client configuration](../README.md#mcp-client-configuration).

Add the document directory as an `--allowed-root`. Configure reviewed templates separately with `--trusted-template-root`; no templates are trusted by default. See [template execution rules](architecture.md#template-execution) before granting access.

## Configuration

CLI flags override corresponding environment variables. Multiple roots use the platform's path-list separator: `;` on Windows and `:` on POSIX. Use absolute paths for client configurations so their working directory cannot change which files are authorized.

| CLI flag | Environment variable | Purpose/default |
| --- | --- | --- |
| `--allowed-root` | `EDITOR010_ALLOWED_ROOTS` | Disk and named-buffer roots; defaults to the working directory. |
| `--trusted-template-root` | `EDITOR010_TRUSTED_TEMPLATE_ROOTS` | Authorized `.bt` roots; empty by default. |
| `--editor-exe` | `EDITOR010_EDITOR_EXE` | Installed executable used for existing-instance forwarding. |
| `--editor-pid` | `EDITOR010_EDITOR_PID` | Pin the intended existing process. |
| `--bridge-dir` | `EDITOR010_BRIDGE_DIR` | Optional local directory for private request files. |
| `--editor-timeout` | `EDITOR010_EDITOR_TIMEOUT` | Reply timeout; 15 seconds. |
| `--preview-ttl-seconds` | `EDITOR010_PREVIEW_TTL_SECONDS` | Preview token lifetime; 300 seconds. |
| `--max-read-bytes` | `EDITOR010_MAX_READ_BYTES` | Returned byte interval; 65,536 bytes. |
| `--max-patch-bytes` | `EDITOR010_MAX_PATCH_BYTES` | Replacement bytes; 65,536 bytes. |
| `--max-scan-bytes` | `EDITOR010_MAX_SCAN_BYTES` | Hash/search/diff input; 268,435,456 bytes. |
| `--max-copy-bytes` | `EDITOR010_MAX_COPY_BYTES` | Disk patch source; 1,073,741,824 bytes. |
| `--max-results` | `EDITOR010_MAX_RESULTS` | Search matches or difference ranges; 1,000. |
| `--chunk-size` | `EDITOR010_CHUNK_SIZE` | Stream chunk; 1,048,576 bytes. |
| `--max-xml-bytes` | `EDITOR010_MAX_XML_BYTES` | Template XML output; 1,048,576 bytes. |

Use `pnpm start --help` for the full CLI. Request files can contain source bytes; choose a private local directory rather than a shared or synchronized folder. For oversized scans, request a smaller interval or raise the local limit deliberately.

## Verify the real GUI

The verifier opens a disposable file, changes one byte without saving, runs the tools through an MCP client, and removes its test files. It restores the original active document. Avoid editing or switching tabs during the run.

Inspect its options using the project environment:

```powershell
pnpm exec uv run --frozen python -m editor010_mcp.verify_live --help
```

Run it explicitly against an already-running editor (replace the example path/PID):

```powershell
pnpm exec uv run --frozen python -m editor010_mcp.verify_live --create-fixture --editor-exe 'C:\Program Files\010 Editor\010Editor.exe' --editor-pid 1234 --work-dir .local/live
```

Success requires `status: passed`, `cleanup: passed` and `process_preserved: true`. The [latest run passed](verification.md). The verifier checks:

1. Record the existing process lifetime and original active document.
2. Create an ordinary disposable fixture, open only that fixture, and record its disk bytes.
3. Change a known fixture byte in the editor without saving. Confirm dirty state.
4. Call the bridge context/read path and verify that it returns the changed byte while the source file still contains the original byte.
5. Select a fixture byte range and confirm the returned selection.
6. Run a reviewed fixture `.bt` and validate its exported XML.
7. Check expected-byte conflicts and preview behavior, then close the owned fixture and restore the original active document.

To include the GUI test in pytest, set `EDITOR010_RUN_LIVE=1` and `EDITOR010_EDITOR_EXE`, plus `EDITOR010_EDITOR_PID` if pinning a process, then run `pnpm test`. Without that opt-in, pytest skips the GUI test.

## Use the live tools

Begin with `server_status` and `editor_status`. Keep the intended ordinary document active, obtain its `buffer_id` from `editor_context`, and pass that value to `editor_read`, `editor_select`, `editor_run_template`, or `editor_patch_preview`. If you switch documents, obtain a fresh context. All byte offsets are zero-based local offsets; a custom hex display address does not change the tool arguments.

`editor_read` returns the editor's current data, including unsaved edits. It never substitutes source-file bytes. `editor_select` changes selection. Template execution changes Template Results and may expose a permission dialog if the trusted template requests additional access. Context and read tools preserve the source bytes and selection.

Editor patch apply returns `CAPABILITY_UNSUPPORTED`; see the [mutation boundary](architecture.md#editor-mutation-boundary). Use the disk backend to create a patched copy, or apply the reviewed edit manually in the editor.

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

Before retrying a timed-out selection or template, inspect its effect in the editor. Reconnect after resolving any running script or dialog. [Timeout behavior](architecture.md#existing-session-bridge).

## Temporary-file cleanup

The default request directory is `%LOCALAPPDATA%\editor010-mcp` on Windows. Handled requests remove their temporary directories; lock files contain no binary data and may remain. After a crash, stop the MCP server and wait for editor scripts to finish before removing identified stale `request-*` directories.

Verifier artifacts live under `--work-dir`. Cleanup failure returns `artifacts_retained: true` with a typed `cleanup_error`. Close the reported run's `probe.bin` before removing its `verification-*` directory.
