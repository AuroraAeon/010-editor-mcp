# 010 Editor MCP Bridge — Agent Instructions

## Mission

Build **010 Editor MCP Bridge**: a local Model Context Protocol (MCP) server that gives coding agents a unified interface to **(1) analyze and modify binary files independently** and **(2) interact with a user's existing 010 Editor session** through supported automation mechanisms.

The intended experience: an agent can examine a binary file without opening the editor, then inspect the selected bytes or run a Binary Template in 010 Editor, and optionally propose a change that appears in the editor's **unsaved buffer** for human review. The two backends must be complementary, not separate products.

**Repository:** `010-editor-mcp`  
**Python import/package:** `editor010_mcp`  
**Initial target:** Windows 11; keep the standalone backend portable.  
**License/branding:** This is an independent integration, not an official SweetScape product. Do not imply endorsement.

## How to work on this project

You are the implementing software-engineering agent. Take this document as authorization to make ordinary, reversible repository changes, install project-local development dependencies when permitted by the environment, run disposable local tests, and fix failures. **Build working software, not just an architecture proposal.** Make reasonable engineering decisions without asking for routine approval. Ask only when a genuine product choice, inaccessible external dependency, or destructive action changes the intended outcome.

Before committing to a GUI integration design, check the current official 010 Editor documentation and perform a minimal working experiment on the available system. Record what is **documented**, **experimentally verified**, and **unverified**. Never label a mock bridge as a live integration. If 010 Editor or a Windows GUI is absent, implement the portable components and a tested bridge contract, and document the exact Windows verification steps and remaining blocker. Do not pretend an unavailable capability was tested.

Keep instructions and code straightforward. Use subagents only when the environment supports them and two genuinely independent tasks can run in parallel without conflicting edits. Run targeted tests after material changes; do not repeatedly rerun unaffected suites without cause. Continue toward the acceptance criteria rather than stopping after scaffolding or the first passing test.

For documentation changes, follow [docs/writing-guide.md](docs/writing-guide.md). Use [docs/README.md](docs/README.md) to find the document that owns each topic.

## Product capabilities

### A. Standalone binary backend (works without 010 Editor)

Support bounded, explicit operations on ordinary files:

- Inspect file metadata, size, and optionally hashes.
- Read bytes by **zero-based byte offset** and length; return hex and an optional safe text preview.
- Search for a hex byte pattern with bounded results.
- Compare two files and return bounded, precise ranges of differences.
- Preview a byte replacement, including before/after bytes and affected offsets.
- Apply a validated replacement to a **new output file by default**, with a precondition that the original bytes match `expected_hex`.
- Make operations practical on large files: seek/stream instead of loading whole files when feasible; impose configurable output and work limits.

Do not promise arbitrary binary-format inference. AI interpretation sits above these deterministic primitives.

### B. Live 010 Editor backend

Target the **currently running 010 Editor GUI session**, not merely a separately launched process. Expose capabilities progressively when actually proven:

- Detect the bridge/editor availability and report the mechanism and limitations.
- Identify the active file/buffer, selected byte range, and modified/dirty state when available.
- Read bytes from the **editor's in-memory buffer**, including unsaved changes, without first saving the source document.
- Select/navigate to a byte range in the user's editor.
- Execute a trusted `.bt` Binary Template on the intended document and retrieve structured results (for example through XML export).
- Preview a replacement and apply it to the **unsaved editor buffer** without triggering Save.

A read from the filesystem is **not** a valid substitute for a live-buffer read; return an explicit capability error when the required live state is unavailable. Do not silently open a second editor instance or alter the user's active document to emulate success.

### C. Unified MCP contract

Offer a single local MCP server with consistent naming, data types, error semantics, and discoverable backend capabilities. Prefer initial **stdio transport** for local coding-agent clients. The standalone backend must start and work when 010 Editor is not installed.

Indicative tools (refine names and arguments against the actual MCP SDK):

| Group | Proposed tools | Behavior |
| --- | --- | --- |
| Health | `server_status`, `editor_status` | Backend and feature availability, clear diagnostics |
| Standalone | `binary_inspect`, `binary_read`, `binary_search`, `binary_diff` | Read-only deterministic binary operations |
| Standalone mutation | `binary_patch_preview`, `binary_patch_apply` | Expected-byte verification; write copy by default |
| Editor | `editor_context`, `editor_read`, `editor_select`, `editor_run_template` | Existing GUI session; never silently fall back to disk |
| Editor mutation | `editor_patch_preview`, `editor_patch_apply` | Unsaved-buffer edits only, with precondition checks |

Expose only implemented tools as usable. If exposing an unavailable operation for discovery, return a typed `BACKEND_UNAVAILABLE` or `CAPABILITY_UNSUPPORTED` error with an actionable reason.

For results, include `source` (`disk` or `editor`), stable file/buffer identity where feasible, canonical zero-based offsets, lengths, and bounded byte representations. Distinguish 010 Editor display addresses (which may have a custom starting address) from file-local byte offsets. Never imply disk data and a dirty editor buffer are identical.

## Architecture and technical defaults

Use **Python 3.11+** and the **current stable official MCP Python SDK**, verifying its installed API before coding. Prefer modest dependencies and a standard `pyproject.toml`; use `pytest` for tests. The bridge may require `.1sc` scripts and an optional local IPC component; keep those isolated from the portable binary engine.

Suggested modules; adjust only when a simpler design is demonstrably better:

```text
010-editor-mcp/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── src/editor010_mcp/
│   ├── server.py             # MCP registration and input validation
│   ├── models.py             # offsets, result types, error codes
│   ├── standalone.py         # portable disk backend
│   ├── editor_backend.py     # live-editor contract / orchestration
│   └── bridge_protocol.py    # request/response transport and correlation
├── bridge/scripts/          # versioned 010 Script (*.1sc) code
├── tests/                    # deterministic disposable fixtures and mocks
└── docs/                     # live bridge investigation and setup
```

Do not couple the MCP tool definitions to the implementation of a particular bridge transport. Serialize editor requests, set timeouts, and match replies using request IDs; 010 Editor may serialize Script/Template execution. Define clear behavior for editor busy, timeout, disconnect, stale selection, and unsupported editor versions.

### Verify the GUI bridge before depending on it

Investigate these options in order of reliability, using real evidence:

1. 010 Editor's documented command-line `-script:` execution and whether it can target **the already running GUI instance**, preserve the user's unsaved buffer, and return correlated results.
2. A user-installed `.1sc` bridge script plus a local request/response channel (for example, temporary files or IPC), provided script execution does not block or destabilize the editor.
3. A narrowly scoped native helper only if needed and justified. Do not start with UI click automation, DLL injection, or process-memory manipulation.

Useful documented functions include `GetFileName`, `GetFileNum`, `FileCount`, `FileSelect`, `GetSelStart`, `GetSelSize`, `FileSaveRange`, `RunTemplate`, `ExpandAll`, and `ExportXML`; verify signatures, version requirements, and behavior in the installed editor. `FileSaveRange` may be useful for exporting in-memory bytes to a **temporary copy**, but test that it captures unsaved edits and does not save the source file. Check template permissions before running untrusted content.

A **mandatory spike** should answer: Can a local agent send a command to an already open 010 Editor window, identify its active document, retrieve an unsaved changed byte, and return a reliable response? Record the steps, editor version, observed behavior, and failure modes. This question determines the live backend implementation; a successful subprocess exit alone is insufficient evidence.

## Editing, safety, and correctness

- Read-only tools never mutate source files, GUI selection, or editor buffers unless that side effect is part of the explicitly named tool (e.g., `editor_select`).
- Preview before mutation. All mutation tools require explicit target identity, offset, replacement, and an expected-byte precondition; reject mismatches instead of overwriting newer data.
- For disk writes, default to a new output path; in-place overwrite requires a separately explicit option, appropriate backup/atomicity strategy, and authorization. Never write outside user-allowed paths.
- For editor writes, modify only the intended buffer, leave it unsaved, and never call `FileSave` implicitly. Do not destroy undo/history or close a modified file as a side effect.
- Keep raw devices, live process memory, and arbitrary scripts supplied by untrusted callers **out of the initial public MCP surface**. Only run templates from configured trusted locations, or require distinct explicit authorization for other locations.
- Enforce valid nonnegative offsets, bounds, maximum read sizes, bounded search/diff output, input hex validation, path normalization, timeouts, and safe temporary-file cleanup.
- Treat editor state as concurrent and mutable. Re-read or validate bytes immediately before patching; report conflicts. Use an explicit unsupported result if atomic validation is impossible.
- Do not log raw private binary content by default. Never transmit file contents to a remote server as part of the bridge.

## Implementation sequence

1. **Foundation and evidence.** Inspect the environment and official docs; create the package, entry point, capability model, typed errors, fixtures, and a short live-bridge feasibility note. Attempt the mandatory GUI spike when an editor is available.
2. **Standalone MVP.** Implement and test inspection, bounded reads/search/diff, preview, safe copy-on-write patches, and MCP registration. Verify the MCP tools through an actual MCP client/inspector if available, not merely direct Python calls.
3. **Editor integration.** Implement the proven bridge transport and working editor-context/read/template/select operations. Add unsaved-buffer patching only after safe addressing and conflict checks are demonstrated. If a GUI cannot be accessed, ship a clean unavailable backend with tests and precise local verification instructions.
4. **Hardening and UX.** Handle reconnects and concurrency, improve errors, add executable Windows setup steps, and supply end-to-end examples combining the two backends. Keep all claimed capabilities aligned with observed tests.

Do not silently turn this into a standalone-only project: the live 010 Editor integration remains a first-class objective even if an environmental limitation prevents its verification in this run.

## Acceptance criteria

The implementation is ready to describe as a useful initial release when:

1. A local MCP client can launch the server and discover/use the standalone tools **without 010 Editor installed**.
2. Deterministic tests cover exact byte offsets, endian-independent raw byte handling, empty files, bounds, malformed hex, missing paths, large-file behavior, and expected-byte conflicts. The original file stays unchanged when writing to a new copy.
3. With a real Windows 010 Editor GUI, the integration demo can identify the active document, read an **unsaved** edit, and run a trusted Binary Template with machine-readable results. Claims about selection and patching require corresponding real GUI evidence.
4. A bridge failure returns a clear error and never silently reads stale disk bytes in place of dirty editor bytes.
5. The README explains installation, local MCP client configuration, editor bridge setup, example prompts, security behavior, supported versions, and which features are verified versus experimental.
6. Tests and representative demo commands actually run. Report **passed**, **failed**, and **not run (with reason)** separately. Do not claim tests pass based only on inspection.

## Your next action

Implement the best complete, verifiable version of this project that the current environment permits. Start with environment discovery and the 010 Editor bridge feasibility spike, then build the standalone server and the integration layer. Keep a brief evidence log in `docs/bridge-feasibility.md`. At the end, report the usable tools, setup/run commands, test evidence, and any live GUI capability that still requires a Windows validation. Do not stop at a plan when implementation is possible.

## Authoritative references

- 010 Editor manual — command-line parameters: https://www.sweetscape.com/010editor/manual/CommandLine.htm
- 010 Editor manual — interface/script functions: https://www.sweetscape.com/010editor/manual/FuncInterface.htm
- 010 Editor manual — writing scripts: https://www.sweetscape.com/010editor/manual/IntroScripts.htm
- 010 Editor manual — editing template variables: https://www.sweetscape.com/010editor/manual/EditingWithScripts.htm
- Official MCP Python SDK: https://py.sdk.modelcontextprotocol.io/
- OpenAI GPT-6 family guidance (includes GPT-6.1 Sol): https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6.1-sol
