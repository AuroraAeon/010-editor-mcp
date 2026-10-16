# Verification results

Date: 2026-10-16. Local runtime: Windows, CPython 3.12.14, official MCP SDK 2.3.0.

## Passed

- `pnpm lint`: Ruff checks passed.
- `pnpm test`: **287 passed, 1 skipped**. The skipped test is the explicit opt-in
  GUI test; ordinary tests never control the editor.
- The suite includes disk edge cases, sparse large files, exact zero-based
  offsets, malformed inputs, path policy, expected-byte conflicts, preview
  replay/expiry, source preservation, transport correlation, timeouts,
  cancellation cleanup, Windows lock contention, trusted templates, XML bounds,
  changed response identities, and verifier lifecycle/cleanup failures.
- The 16 MCP tests launch the real stdio server and use the official client.
  Both current discovery and legacy initialization passed. The same **16 tests
  passed against an isolated installed wheel**, imported from site-packages.
- Wheel build and inclusion of `bridge_scripts/command.1sc` passed. No editor
  installation is needed for standalone operation.
- The final wheel (including staged-reply publication) was reinstalled into a
  fresh isolated environment. Package/resource lookup, 14-tool discovery,
  a byte read through MCP, and unavailable-editor reporting all passed.
- [GitHub Actions run 38844282123](https://github.com/AuroraAeon/010-editor-mcp/actions/runs/38844282123)
  passed on implementation commit `7c0b37d`: Windows/Linux × Python 3.11/3.13,
  including lint, the portable suite and wheel-resource validation.
- A real 010 Editor 16.0.4 GUI retained the same process while command forwarding
  identified its active document and read an unsaved `AA` byte at offset 1;
  disk still contained `11`. Dirty state, custom display origin, selection
  changes/restoration, and trusted template XML containing decimal `170` were
  observed. The production script's final staging/rename completion signal was
  also exercised with real context and byte-read requests.
- A real MCP client exercised the live context, disk/live divergence, dirty
  state, selection, trusted template, conflict preview, no-mutation preview,
  and explicit unsupported-apply responses before its final selection assertion
  exposed the template-selection behavior described below.

## Failed during investigation

The failures are retained here so a passing mock does not erase runtime evidence.

- Early script probes failed compilation due to `SPrintf` signature misuse and
  adjacent string-literal syntax. Both were corrected and followed by successful
  real script execution.
- The first full verifier did not keep its script-opened fixture as the active
  GUI tab. `FileSelect` is a script I/O target; editor options can show the
  original tab afterward. The verifier now activates its own fixture explicitly
  before setup and uses consistent native filename spelling for lookup.
- A subsequent actual MCP exercise reached its final assertion but found that
  template execution had changed selection. The verifier now explicitly restores
  selection through `editor_select` after the template.
- A later complete verifier attempt timed out during setup and reported cleanup
  failure. Its artifacts were retained locally. Setup lookup failure now produces
  an explicit failed check instead of returning without a response; diagnostic
  handling also unwraps the SDK's nested exception groups.

These final verifier corrections have deterministic lifecycle tests but have
**not** been declared a passing full real-GUI verifier run.

## Not run / remaining verification

The user stopped Computer Use with the physical Escape key during investigation.
No more GUI input or editor-state changes were made afterward. Therefore the
final full verifier and the opt-in real-GUI pytest remain **not run after the
last corrections**. Re-run them in an existing disposable editor session:

```powershell
pnpm exec uv run --frozen python -m editor010_mcp.verify_live --create-fixture --editor-exe 'C:\Program Files\010 Editor\010Editor.exe' --work-dir .local/live
```

For pytest, explicitly set `EDITOR010_RUN_LIVE=1` and
`EDITOR010_EDITOR_EXE` (optionally `EDITOR010_EDITOR_PID`), then run:

```powershell
pnpm exec uv run --frozen pytest tests/test_live_editor.py -v
```

Other GUI versions/platforms and hostile concurrent filesystem mutation are
unverified. Atomic editor patch application is **unsupported**, rather than a
passing or skipped feature. The exact concurrency reason is documented in
[bridge-feasibility.md](bridge-feasibility.md).

Hosted CI is configured for Windows/Linux with Python 3.11/3.13; its live status
is available in the repository's Actions page. It does not run GUI tests.
