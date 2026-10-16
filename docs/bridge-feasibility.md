# Live bridge feasibility evidence

Investigation date: 2026-10-16. Host: Windows; installed 010 Editor 16.0.4
(PE version 16.0.4.0). No user editor process was running at discovery. A
disposable GUI session was launched for the spike; subsequent commands targeted
that same already-running process.

## Documented

- [`-script:` and command forwarding](https://www.sweetscape.com/010editor/manual/CommandLine.htm):
  command-line execution and script arguments are supported. Loading files can
  target an already-running instance.
- [Single-instance setting](https://www.sweetscape.com/010editor/manual/OptionsGeneral.htm):
  keep “Allow Only One Instance of 010 Editor” enabled.
- [Interface functions](https://www.sweetscape.com/010editor/manual/FuncInterface.htm):
  active index/name, dirty state, selection, temporary range export, and template
  execution/XML export are documented. `GetStartingAddress` requires v13+.
- [I/O functions](https://www.sweetscape.com/010editor/manual/FuncIO.htm):
  raw reads and writes use local byte offsets. `FileSaveRange` requires a
  destination different from the current source filename.
- [Execution model](https://www.sweetscape.com/010editor/manual/Running.htm):
  Scripts run in a separate thread while users can edit. Only one Script/Template
  runs at a time. No documented atomic compare-and-write or buffer lifetime ID
  was found in the complete interface, I/O, string, and tool references.

## Experimentally verified

The mandatory experiment used the owned eight-byte fixture `00 11 22 33 44 55 66 77`.

1. Launch one GUI, dismiss its startup notice, then send a separate process
   invocation containing only `-script:<owned probe>`. The launcher exited,
   the existing GUI PID remained the only editor process, and a correlated
   scratch-file response identified the active document.
2. Intentionally open the disposable fixture for this experiment. A probe
   changed offset 1 to `AA` without saving, selected offset 2/length 3, and
   used display origin `0x1000`.
3. `FileSaveRange` exported `00 AA 22 33 44 55 66 77`. The source file remained
   `00 11 22 33 44 55 66 77`. A subsequent script reported `IsModified() == 1`,
   the same active document and selection.
4. The implemented transport's context/read operations reproduced these results
   and preserved selection and display origin. Only the owned scratch response
   buffer was created/closed.
5. `editor_select` selected offset 1/length 2, reported that selection, and
   restored offset 2/length 3 on a subsequent explicit select.
6. `RunTemplate` + `ExpandAll` + `ExportXML` ran the reviewed ByteHeader template
   on that dirty buffer. XML contained byte value `170` at display address
   `1001h`; the server normalized it to file-local offset `1`. The source file
   stayed unchanged and the buffer stayed dirty.
7. The production script with the new-process environment guard still reached
   the existing GUI and returned the dirty buffer, showing that forwarding
   does not inherit the launcher's environment into the original process.

Current results are in [verification.md](verification.md). Versioned scripts are
under `bridge/scripts`; scratch output stays in the ignored `.local` directory.

## Observed failures and corrections

- Launching hidden initially left an invisible main window blocked by a startup
  “Restore Open Files” notice. The owned empty session was restarted visibly;
  the notice was inspected/dismissed. Production discovery refuses missing GUI
  windows, disabled main windows, and nonresponsive windows.
- An initial probe misused `SPrintf` as a string-returning function. Its real
  compiler error was read from the GUI. The probe switched to documented `Str`.
- 010 Script rejected adjacent C string literals in a format argument. Explicit
  `+` concatenation fixed the production script; a real context/read/template
  round-trip then passed.
- `FileOpen`/`FileSelect` inside a setup script did not reliably keep its new
  target as the active GUI tab. The compiling preference can show the original
  target afterward. The verifier now explicitly opens only its disposable file
  through the command line. [Compiling options](https://www.sweetscape.com/010editor/manual/OptionsCompiling.htm).
- Template execution changed the GUI selection in the MCP acceptance exercise.
  The verifier now explicitly restores its fixture selection after execution;
  context/read selection preservation is checked before the template.
- `IsModified()` was still false inside the same script that wrote the probe
  byte. A separate follow-up context correctly reported true. Verification
  checks dirty state after the setup script has completed.
- On this Windows Python runtime, pathname `stat` and handle `fstat` disagree
  about ctime. Disk identities use device/inode/size/mtime on Windows; a
  regression test covers the observed mismatch.
- A Windows byte-range lock originally read its first byte before catching
  contention. The implementation now checks file size without reading that
  locked byte and returns typed `EDITOR_BUSY`; an actual local contention test
  verifies it.

## Full verifier rerun, 2026-10-16

The corrected verifier passed all eight MCP checks on the existing 010 Editor
16.0.4.0 process. The pytest GUI case also passed. The fixture was closed and
removed, and the previous active document was restored.

During the rerun, the old cleanup script returned success while its dirty target
tab remained open. File deletion then raised `PermissionError`. The corrected
cleanup activates the previous document, closes the owned fixture from that
separate script target, checks `FindOpenFile`, and confirms the restored context.
It waits for Windows handles to release before removing files and reports the
cleanup phase if a failure remains. When the editor starts empty, an owned empty
anchor provides the separate cleanup target.

The passing report contains `status: passed`, `cleanup: passed`, and
`process_preserved: true`. [Run command](editor-setup.md#verify-the-real-gui).

An empty-session check found that `FPrintf` requires an active script I/O target.
Response writers now select their owned scratch buffer before writing, allowing
the bridge to return `NO_ACTIVE_BUFFER` when no source document is open.

## Unsupported or unverified

**Unsaved-buffer patch apply is unsupported.** A sequential read/check/write can
race user edits. `SetReadOnly` and native replacement functions have no documented
atomic guarantee. The public tool returns `CAPABILITY_UNSUPPORTED`. The
[architecture](architecture.md#editor-mutation-boundary) defines this rule.

Context tokens bind session PID/creation-time, active index, filename and size.
They cannot detect closing and reopening the same path with identical size.
They expire, and changed active context is rejected; they are not persistent
editor object IDs. Untitled buffers are explicitly unsupported for addressed
operations until named. Concurrent user activity can affect observations.

Other editor versions, multiple GUI sessions, Linux/macOS GUI forwarding,
untrusted templates, edit-lock behavior, exhaustive export of lazy/suppressed
variables, and arbitrarily long template cancellation are unverified. The
standalone backend and unavailable-editor behavior remain independent of these.
