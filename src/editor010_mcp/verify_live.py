"""Opt-in disposable real-GUI acceptance through an actual MCP stdio client."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .bridge_protocol import BridgeTransport, _dispatch, _process_lock
from .editor_backend import EditorBackend
from .models import BridgeError, Settings
from .windows import EditorProcess, discover

DISK = bytes.fromhex("0011223344556677")
LIVE = bytes.fromhex("00aa223344556677")


def _require(condition: Any, check: str) -> None:
    if not condition:
        raise BridgeError("VERIFICATION_FAILED", check)


def _remove_owned_file(path: Path, timeout: float) -> None:
    """Wait for 010 Editor to release a just-closed verifier file on Windows."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.05, remaining))


def _editor_command(settings: Settings, process: EditorProcess, *args: str) -> None:
    """Run verifier-only document commands in the pinned existing process."""
    _require(discover(settings).identity == process.identity, "editor_session_changed")
    subprocess.run(
        [str(process.exe), *args],
        check=True,
        timeout=settings.editor_timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _require(discover(settings).identity == process.identity, "editor_session_changed")


def _probe(settings: Settings, process: EditorProcess, work: Path, body: str) -> None:
    """Trusted verifier-only setup/cleanup, never an exposed MCP script tool."""
    transport = BridgeTransport(settings)
    transport.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (
        _process_lock(transport.directory / f"session-{process.pid}-{process.started}.lock"),
        tempfile.TemporaryDirectory(prefix="probe-", dir=work) as directory,
    ):
        folder = Path(directory)
        marker, reply = folder / "pending", folder / "reply.json"
        marker.touch()

        def quote(path: Path) -> str:
            return json.dumps(path.as_posix(), ensure_ascii=False)

        source = (
            "RequiresVersion(13,0);\n"
            'if(GetEnv("EDITOR010_MCP_NEW_PROCESS")=="1") return;\n'
            f"if(!FileExists({quote(marker)})) return;\n" + body + "\nint target=GetFileNum();\n"
            'string response=Str("{\\"ok\\":%d}",check);\n'
            'int output=FileNew("Hex",false);\n'
            'FileSelect(output);\nFPrintf(output,"%s",response);\n'
            f'FileSaveRange({quote(reply)}+".staging",0,FileSize());\n'
            "FileClose();\nif(target>=0) FileSelect(target);\n"
            f'RenameFile({quote(reply)}+".staging",{quote(reply)});\n'
        )
        script = folder / "probe.1sc"
        script.write_text(source, encoding="utf-8")
        try:
            _require(discover(settings).identity == process.identity, "editor_session_changed")
            deadline = time.monotonic() + settings.editor_timeout
            _dispatch(process.exe, script, settings.editor_timeout)
            while time.monotonic() < deadline:
                if reply.is_file():
                    try:
                        result = json.loads(reply.read_text(encoding="utf-8"))
                    except (ValueError, UnicodeError):
                        pass  # Export may still be in progress.
                    else:
                        _require(result.get("ok") == 1, "fixture_setup_or_restore_failed")
                        _require(
                            discover(settings).identity == process.identity,
                            "editor_session_changed",
                        )
                        return
                time.sleep(0.05)
            raise BridgeError("EDITOR_TIMEOUT", "Verifier probe did not return a reply.")
        finally:
            marker.unlink(missing_ok=True)


async def _call(client: Client, name: str, args: dict, error: str | None = None) -> dict:
    result = await client.call_tool(name, args)
    value = result.structured_content
    _require(isinstance(value, dict), "mcp_result_has_no_structured_content")
    if error:
        _require(result.is_error and value.get("error", {}).get("code") == error, name)
    else:
        _require(not result.is_error, name)
    return value


async def _exercise(
    settings: Settings, process: EditorProcess, fixture: Path, template: Path
) -> list:
    env = {key: value for key, value in os.environ.items() if not key.startswith("EDITOR010_")}
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "editor010_mcp",
            "--allowed-root",
            str(fixture.parent),
            "--trusted-template-root",
            str(template.parent),
            "--editor-exe",
            str(process.exe),
            "--editor-pid",
            str(process.pid),
            "--editor-timeout",
            str(settings.editor_timeout),
        ],
        cwd=fixture.parent,
        env=env,
    )
    checks = []
    async with Client(params, read_timeout_seconds=max(30, settings.editor_timeout * 2)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        _require({"editor_context", "editor_read", "binary_read"} <= names, "mcp_discovery")
        status = await _call(client, "editor_status", {})
        _require(status["available"] and status["pid"] == process.pid, "existing_process")
        context = await _call(client, "editor_context", {})
        _require(Path(context["path"]).resolve() == fixture, "active_fixture")
        _require(context["dirty"], "unsaved_dirty_state")
        _require(context["display_start_address"] == 4096, "display_address_is_separate")
        target = {"buffer_id": context["buffer_id"]}
        read_args = {**target, "offset": 0, "length": len(LIVE)}
        read = await _call(client, "editor_read", read_args)
        disk = await _call(
            client, "binary_read", {"path": str(fixture), "offset": 0, "length": len(DISK)}
        )
        _require(read["source"] == "editor" and read["hex"] == LIVE.hex(), "unsaved_live_read")
        _require(disk["source"] == "disk" and disk["hex"] == DISK.hex(), "unchanged_disk_read")
        after = await _call(client, "editor_context", {})
        _require(after["selection"] == context["selection"], "read_preserves_selection")
        checks.extend(["mcp_stdio_discovery", "unsaved_live_read", "unchanged_disk_read"])
        try:
            selected = await _call(client, "editor_select", {**target, "offset": 1, "length": 2})
            _require(selected["selection"] == {"offset": 1, "length": 2}, "selection")
        finally:
            await _call(client, "editor_select", {**target, **context["selection"]})
        checks.append("selection_roundtrip")
        exported = await _call(
            client, "editor_run_template", {**target, "template_path": str(template)}
        )
        xml = ET.fromstring(exported["xml"])
        values = {node.findtext("name"): node.findtext("value") for node in xml.iter("variable")}
        _require(values.get("bridge_probe_changed") == "170", "template_contains_unsaved_byte")
        checks.append("trusted_template_xml_from_unsaved_buffer")
        # Template execution may select a parsed field in the GUI. Restore only
        # through the explicitly named selection tool in this disposable demo.
        await _call(client, "editor_select", {**target, **context["selection"]})
        patch = {**target, "offset": 1, "expected_hex": "aa", "replacement_hex": "ab"}
        await _call(
            client, "editor_patch_preview", {**patch, "expected_hex": "00"}, "PRECONDITION_FAILED"
        )
        preview = await _call(client, "editor_patch_preview", patch)
        _require(preview["can_apply"] is False, "patch_apply_capability")
        await _call(
            client,
            "editor_patch_apply",
            {**patch, "preview_token": "unsupported"},
            "CAPABILITY_UNSUPPORTED",
        )
        _require(
            (await _call(client, "editor_read", read_args))["hex"] == LIVE.hex(), "no_mutation"
        )
        _require(fixture.read_bytes() == DISK, "source_file_changed")
        final = await _call(client, "editor_context", {})
        _require(final["selection"] == context["selection"], "selection_was_not_restored")
        checks.extend(
            ["expected_byte_conflict", "preview_without_mutation", "apply_explicitly_unsupported"]
        )
    _require(discover(settings).identity == process.identity, "editor_session_changed")
    return checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--create-fixture",
        action="store_true",
        required=True,
        help="Explicitly authorize opening, modifying and closing only the disposable probe file.",
    )
    parser.add_argument("--editor-exe", type=Path, required=True)
    parser.add_argument("--editor-pid", type=int)
    parser.add_argument("--work-dir", type=Path, default=Path(".local/live"))
    parser.add_argument("--editor-timeout", type=float, default=15)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    settings = Settings(
        editor_exe=args.editor_exe,
        editor_pid=args.editor_pid,
        editor_timeout=args.editor_timeout,
    )
    report: dict[str, Any] = {"status": "failed", "transport": "mcp-stdio", "checks": []}
    owned: Path | None = None
    previous: dict | None = None
    prepared = False
    process: EditorProcess | None = None
    try:
        process = discover(settings)  # No executable is launched unless this succeeds.
        backend = EditorBackend(settings)
        try:
            previous = backend.context()
        except BridgeError as exc:
            if exc.code != "NO_ACTIVE_BUFFER":
                raise
        work = args.work_dir.expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
        owned = Path(tempfile.mkdtemp(prefix="verification-", dir=work)).resolve()
        fixture, template = owned / "probe.bin", owned / "probe.bt"
        fixture.write_bytes(DISK)
        template.write_text(
            "RequiresVersion(13,0);\nuchar bridge_probe_first;\n"
            "uchar bridge_probe_changed;\nuchar bridge_probe_rest[6];\n",
            encoding="utf-8",
        )
        # Match the filename spelling passed to the command-line opener. Some
        # editor lookups retain slash spelling rather than canonicalizing it.
        quoted = json.dumps(str(fixture), ensure_ascii=False)
        prepared = True  # Cleanup also runs after partial setup failures.
        # FileSelect is the script's I/O target; GUI options may restore the
        # original tab after it exits. Explicitly activate only our owned file.
        _editor_command(settings, process, str(fixture))
        _probe(
            settings,
            process,
            owned,
            f"int fixture=FindOpenFile({quoted});\n"
            "int check=0;\nif(fixture>=0 && GetFileNum()==fixture) {\n"
            "WriteUByte(1,0xAA);\nSetStartingAddress(4096);\n"
            "SetSelection(2,3);\ncheck=1;}\n",
        )
        report["checks"] = asyncio.run(_exercise(settings, process, fixture, template))
        report.update(status="passed", editor_version=process.version, process_preserved=True)
    except BridgeError as exc:
        report["error"] = {"code": exc.code, "check": exc.message}
    except Exception as exc:

        def cause(error):
            if isinstance(error, BaseExceptionGroup):
                return cause(error.exceptions[0])
            return error

        nested = cause(exc)
        report["error"] = (
            {"code": nested.code, "check": nested.message}
            if isinstance(nested, BridgeError)
            else {"code": "VERIFICATION_FAILED", "exception": type(nested).__name__}
        )
    finally:
        if prepared and owned is not None and process is not None:
            cleanup_phase = "close_fixture"
            try:
                quoted = json.dumps(str(owned / "probe.bin"), ensure_ascii=False)
                # Close the fixture from another script target so the editor's
                # Show Target File preference cannot reactivate the closed tab.
                anchor = owned / "cleanup.bin"
                cleanup_target = previous["path"] if previous is not None else str(anchor)
                if previous is None:
                    anchor.write_bytes(b"")
                _editor_command(settings, process, cleanup_target)
                restore = ""
                if previous is not None:
                    name = json.dumps(previous["path"], ensure_ascii=False)
                    restore = (
                        f"int restore=FindOpenFile({name});\n"
                        "if(restore>=0) {FileSelect(restore);\n"
                        f"if(GetFileName()!={name}) check=0;}} else check=0;\n"
                    )
                cleanup_phase = "verify_closed"
                _probe(
                    settings,
                    process,
                    owned,
                    f"int owned=FindOpenFile({quoted});\n"
                    f"if(owned>=0) {{FileSelect(owned); if(GetFileName()!={quoted}) return;"
                    "FileClose();}\n"
                    f"int check=FindOpenFile({quoted})<0;\n" + restore,
                )
                cleanup_phase = "restore_document"
                if previous is not None:
                    current = EditorBackend(settings).context()
                    _require(current["path"] == previous["path"], "original_document_not_restored")
                else:
                    current = EditorBackend(settings).context()
                    _require(Path(current["path"]).resolve() == anchor, "cleanup_anchor_not_active")
                    _editor_command(settings, process, "-close")
                    _probe(
                        settings,
                        process,
                        owned,
                        f"int check=FindOpenFile({json.dumps(str(anchor))})<0;\n",
                    )
                cleanup_phase = "remove_fixture"
                for name in ("probe.bin", "probe.bt", "cleanup.bin"):
                    _remove_owned_file(owned / name, settings.editor_timeout)
                cleanup_phase = "remove_directory"
                owned.rmdir()
                report["cleanup"] = "passed"
            except Exception as exc:
                report.update(status="failed", cleanup="failed", artifacts_retained=True)
                if isinstance(exc, BridgeError):
                    report["cleanup_error"] = {"code": exc.code, "check": exc.message}
                else:
                    report["cleanup_error"] = {
                        "code": "CLEANUP_FAILED",
                        "phase": cleanup_phase,
                        "exception": type(exc).__name__,
                    }
        print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
