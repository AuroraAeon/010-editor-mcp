# Verification results

Date: 2026-10-16. Local runtime: Windows, CPython 3.12.14, official MCP SDK 2.3.0.

## Passed

- `pnpm lint`: Ruff checks passed.
- `pnpm test`: **291 passed** with `EDITOR010_RUN_LIVE=1`, including the opt-in
  real-GUI test. The default suite has 290 passed and one skipped GUI test.
- Coverage includes disk edge cases, sparse files, byte offsets, malformed
  inputs, path policy, conflicts, preview expiry, source preservation, transport
  errors, Windows lock contention, trusted XML and verifier cleanup.
- The 16 MCP tests launch the real stdio server and use the official client.
  Both current discovery and legacy initialization passed. The same **16 tests
  passed against an isolated installed wheel**, imported from site-packages.
- Wheel build and inclusion of `bridge_scripts/command.1sc` passed.
- An isolated installation of the rebuilt wheel included the corrected response
  writer for empty sessions. Markdown links, heading anchors and both README JSON
  configurations passed validation.
- The final wheel (including staged-reply publication) was reinstalled into a
  fresh isolated environment. Package/resource lookup, 14-tool discovery,
  a byte read through MCP, and unavailable-editor reporting all passed.
- [GitHub Actions run 38844282123](https://github.com/AuroraAeon/010-editor-mcp/actions/runs/38844282123)
  passed on implementation commit `7c0b37d`: Windows/Linux × Python 3.11/3.13,
  including lint, the portable suite and wheel-resource validation.
- The corrected full GUI verifier passed through the official MCP stdio client:
  `mcp_stdio_discovery`, `unsaved_live_read`, `unchanged_disk_read`,
  `selection_roundtrip`, `trusted_template_xml_from_unsaved_buffer`,
  `expected_byte_conflict`, `preview_without_mutation`, and
  `apply_explicitly_unsupported`. Cleanup closed and removed only the owned
  fixture, restored the original active document, preserved the editor process,
  and returned `status: passed` on 010 Editor 16.0.4.0.
- The full verifier also passed from an empty editor session. It removed both
  the probe and its cleanup anchor and left no source document open.

At offset 1, the live buffer contained `AA` while disk retained `11`. The template
XML reported decimal `170` at display address `1001h`, normalized to local offset
`1`. Read operations preserved selection; the verifier explicitly restored it
after template execution.

## Reproduce

Use an existing editor process and the installed executable path:

```powershell
pnpm exec uv run --frozen python -m editor010_mcp.verify_live --create-fixture --editor-exe 'C:\Program Files\010 Editor\010Editor.exe' --work-dir .local/live
```

For pytest, set `EDITOR010_RUN_LIVE=1` and `EDITOR010_EDITOR_EXE` (optionally
`EDITOR010_EDITOR_PID`), then run:

```powershell
pnpm exec uv run --frozen pytest tests/test_live_editor.py -v
```

## Failures and coverage gaps

Earlier script compilation, target activation and cleanup failures were fixed.
The [feasibility log](bridge-feasibility.md) records their causes and reruns.
The current full verifier and test suite have no outstanding failures.

Other GUI versions/platforms and hostile concurrent filesystem mutation remain
unverified. Editor patch apply returns `CAPABILITY_UNSUPPORTED`; see the
[mutation boundary](architecture.md#editor-mutation-boundary).

Hosted CI is configured for Windows/Linux with Python 3.11/3.13; its live status
is available in the repository's Actions page. It does not run GUI tests.
