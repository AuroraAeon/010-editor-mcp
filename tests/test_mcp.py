"""Exercise real JSON-RPC stdio subprocesses using the official SDK client."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult, TextContent

TOOLS = {
    "server_status",
    "editor_status",
    "binary_inspect",
    "binary_read",
    "binary_search",
    "binary_diff",
    "binary_patch_preview",
    "binary_patch_apply",
    "editor_context",
    "editor_read",
    "editor_select",
    "editor_run_template",
    "editor_patch_preview",
    "editor_patch_apply",
}


def parameters(root: Path, *extra: str) -> StdioServerParameters:
    env = {key: value for key, value in os.environ.items() if not key.startswith("EDITOR010_")}
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "editor010_mcp",
            "--allowed-root",
            str(root),
            "--editor-exe",
            str(root / "missing-010editor.exe"),
            "--editor-pid",
            "2147483000",
            *extra,
        ],
        env=env,
        cwd=root,
    )


def payload(result: CallToolResult, *, error: bool = False) -> dict:
    assert result.is_error is error
    assert isinstance(result.structured_content, dict)
    blocks = [block for block in result.content if isinstance(block, TextContent)]
    assert len(blocks) == 1
    assert json.loads(blocks[0].text) == result.structured_content
    return result.structured_content


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_discovery_and_read_only_operations(tmp_path: Path, mode: str) -> None:
    original = b"\x00\x01ABC\xffABC\x10"
    source = tmp_path / "sample.bin"
    source.write_bytes(original)
    changed = tmp_path / "changed.bin"
    changed.write_bytes(original[:3] + b"Z" + original[4:] + b"\x11")
    async with Client(parameters(tmp_path), mode=mode, read_timeout_seconds=20) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == TOOLS
        tools = {tool.name: tool for tool in listed.tools}
        assert tools["binary_read"].annotations.read_only_hint is True
        assert tools["editor_select"].annotations.read_only_hint is False
        assert "preview_token" in tools["binary_patch_apply"].input_schema["required"]
        status = payload(await client.call_tool("server_status", {}))
        assert status["standalone"]["available"] is True
        assert status["transport"] == "stdio"
        inspected = payload(
            await client.call_tool(
                "binary_inspect",
                {
                    "path": str(source),
                    "hashes": ["sha256"],
                },
            )
        )
        assert inspected["source"] == "disk"
        assert inspected["size"] == len(original)
        assert inspected["hashes"]["sha256"] == hashlib.sha256(original).hexdigest()
        read = payload(
            await client.call_tool(
                "binary_read",
                {
                    "path": str(source),
                    "offset": 2,
                    "length": 4,
                },
            )
        )
        assert read["hex"] == "414243ff"
        assert read["offset"] == 2 and read["length"] == 4
        searched = payload(
            await client.call_tool(
                "binary_search",
                {
                    "path": str(source),
                    "pattern_hex": "41 42 43",
                },
            )
        )
        assert [match["offset"] for match in searched["matches"]] == [2, 6]
        diff = payload(
            await client.call_tool(
                "binary_diff",
                {
                    "path_a": str(source),
                    "path_b": str(changed),
                },
            )
        )
        assert [(entry["offset"], entry["length"]) for entry in diff["ranges"]] == [(3, 1), (10, 1)]
    assert source.read_bytes() == original


async def test_stdio_copy_on_write_patch_and_conflicts(tmp_path: Path) -> None:
    source, output = tmp_path / "source.bin", tmp_path / "output.bin"
    source.write_bytes(b"\x00\x11\x22\x33")
    patch = {"path": str(source), "offset": 1, "expected_hex": "1122", "replacement_hex": "aabb"}
    async with Client(parameters(tmp_path), read_timeout_seconds=20) as client:
        preview = payload(await client.call_tool("binary_patch_preview", patch))
        assert preview["before_hex"] == "1122"
        assert preview["after_hex"] == "aabb"
        apply = {**patch, "output_path": str(output), "preview_token": preview["preview_token"]}
        applied = payload(await client.call_tool("binary_patch_apply", apply))
        assert applied["original_unchanged"] is True
        assert output.read_bytes() == b"\x00\xaa\xbb\x33"
        assert source.read_bytes() == b"\x00\x11\x22\x33"
        replay = payload(await client.call_tool("binary_patch_apply", apply), error=True)
        assert replay["error"]["code"] == "PREVIEW_REQUIRED"
        conflict = payload(
            await client.call_tool(
                "binary_patch_preview",
                {
                    **patch,
                    "expected_hex": "ffff",
                },
            ),
            error=True,
        )
        assert conflict["error"]["code"] == "PRECONDITION_FAILED"
        assert "ffff" not in json.dumps(conflict)


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [
        ("binary_read", {"path": "sample.bin", "offset": -1, "length": 1}, "INVALID_ARGUMENT"),
        ("binary_read", {"path": "sample.bin", "offset": True, "length": 1}, "INVALID_ARGUMENT"),
        ("binary_read", {"path": "sample.bin", "offset": 0.5, "length": 1}, "INVALID_ARGUMENT"),
        (
            "binary_read",
            {"path": "sample.bin", "offset": "PRIVATE-CALLER-DATA", "length": 1},
            "INVALID_ARGUMENT",
        ),
        ("binary_read", {"path": "sample.bin", "offset": 0, "length": 4}, "OUT_OF_BOUNDS"),
        ("binary_read", {"path": "missing.bin", "offset": 0, "length": 1}, "PATH_NOT_FOUND"),
        ("binary_search", {"path": "sample.bin", "pattern_hex": "0xz"}, "INVALID_ARGUMENT"),
    ],
)
async def test_stdio_typed_backend_errors(
    tmp_path: Path, tool: str, arguments: dict, code: str
) -> None:
    (tmp_path / "sample.bin").write_bytes(b"abc")
    async with Client(parameters(tmp_path), read_timeout_seconds=20) as client:
        result = payload(await client.call_tool(tool, arguments), error=True)
        assert result["error"]["code"] == code
        assert "PRIVATE-CALLER-DATA" not in json.dumps(result)
        # The session remains usable after an operation error.
        assert payload(await client.call_tool("server_status", {}))["standalone"]["available"]


async def test_stdio_editor_unavailable_never_falls_back_to_disk(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"SECRET-DISK-CONTENT")
    async with Client(parameters(tmp_path), read_timeout_seconds=20) as client:
        status = payload(await client.call_tool("editor_status", {}))
        assert status["available"] is False
        operations = [
            ("editor_context", {}),
            ("editor_read", {"buffer_id": str(source), "offset": 0, "length": 6}),
            ("editor_select", {"buffer_id": str(source), "offset": 0, "length": 1}),
            ("editor_run_template", {"buffer_id": str(source), "template_path": "missing.bt"}),
            (
                "editor_patch_preview",
                {
                    "buffer_id": str(source),
                    "offset": 0,
                    "expected_hex": "53",
                    "replacement_hex": "54",
                },
            ),
            (
                "editor_patch_apply",
                {
                    "buffer_id": str(source),
                    "offset": 0,
                    "expected_hex": "53",
                    "replacement_hex": "54",
                    "preview_token": "not-a-real-token",
                },
            ),
        ]
        for name, arguments in operations:
            result = payload(await client.call_tool(name, arguments), error=True)
            assert result["error"]["code"] in {"BACKEND_UNAVAILABLE", "CAPABILITY_UNSUPPORTED"}
            assert "SECRET-DISK-CONTENT" not in json.dumps(result)
    assert source.read_bytes() == b"SECRET-DISK-CONTENT"


async def test_stdio_limits_and_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "data.bin"
    source.write_bytes(b"ABCABC")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"PRIVATE")
    async with Client(
        parameters(allowed, "--max-read-bytes", "2", "--max-scan-bytes", "3"),
        read_timeout_seconds=20,
    ) as client:
        for tool, arguments, code in [
            ("binary_read", {"path": str(source), "offset": 0, "length": 3}, "LIMIT_EXCEEDED"),
            ("binary_search", {"path": str(source), "pattern_hex": "41"}, "LIMIT_EXCEEDED"),
            ("binary_read", {"path": str(outside), "offset": 0, "length": 1}, "PATH_NOT_ALLOWED"),
        ]:
            failed = payload(await client.call_tool(tool, arguments), error=True)
            assert failed["error"]["code"] == code
        successful = payload(
            await client.call_tool(
                "binary_search",
                {
                    "path": str(source),
                    "pattern_hex": "41",
                    "length": 3,
                },
            )
        )
        assert successful["matches"] == [{"offset": 0, "length": 1}]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EDITOR010_MAX_READ_BYTES", "private-invalid-value"),
        ("EDITOR010_EDITOR_TIMEOUT", "nan"),
        ("EDITOR010_PREVIEW_TTL_SECONDS", "inf"),
        ("EDITOR010_EDITOR_PID", "0"),
    ],
)
def test_invalid_environment_exits_cleanly_without_traceback(
    tmp_path: Path, name: str, value: str
) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("EDITOR010_")}
    env[name] = value
    result = subprocess.run(
        [sys.executable, "-m", "editor010_mcp"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert not result.stdout
    assert "Traceback" not in result.stderr
    assert "private-invalid-value" not in result.stderr
    assert json.loads(result.stderr)["error"]["code"] == "INVALID_ARGUMENT"
