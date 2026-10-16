"""MCP stdio transport; deterministic backends remain independent of the SDK."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

from . import __version__
from .editor_backend import EditorBackend
from .models import BridgeError, Settings
from .standalone import BinaryEngine

StrictInt = Annotated[int, Field(strict=True)]
StrictBool = Annotated[bool, Field(strict=True)]


def _result(value: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=json.dumps(value, ensure_ascii=True))],
        structured_content=value,
        is_error=error,
    )


def _invoke(operation: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> CallToolResult:
    """Return expected failures as typed tool results without leaking exception data."""
    try:
        return _result(operation(*args, **kwargs))
    except BridgeError as exc:
        return _result({"error": exc.as_dict()}, error=True)
    except OSError:
        return _result(
            {"error": {"code": "IO_ERROR", "message": "Local I/O failed.", "details": {}}},
            error=True,
        )
    except Exception:
        return _result(
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Operation failed; no source data was logged. Check local setup.",
                    "details": {},
                }
            },
            error=True,
        )


class BridgeMCPServer(MCPServer):
    """Keep SDK schema failures in the same sanitized error contract as backend failures."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> CallToolResult:
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError:
            return _result(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Tool execution failed.",
                        "details": {},
                    }
                },
                error=True,
            )
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                fields = sorted(
                    {
                        ".".join(str(part) for part in item["loc"])
                        for item in exc.__cause__.errors(include_input=False)
                    }
                )
                error = {
                    "code": "INVALID_ARGUMENT",
                    "message": "Tool arguments do not match the schema.",
                    "details": {"fields": fields},
                }
            else:
                error = {
                    "code": "CAPABILITY_UNSUPPORTED",
                    "message": "Requested tool is unavailable.",
                    "details": {},
                }
            return _result({"error": error}, error=True)


def create_server(settings: Settings | None = None) -> MCPServer:
    """Build a stdio-capable server without requiring 010 Editor to be installed."""
    config = settings or Settings.from_env()
    binary = BinaryEngine(config)
    editor = EditorBackend(config)
    server = BridgeMCPServer(
        "010 Editor MCP Bridge",
        version=__version__,
        description="Independent local binary tooling and existing-session 010 Editor bridge.",
        instructions=(
            "Offsets are zero-based file-local byte offsets, not editor display addresses. "
            "Check editor_status before live operations. Editor reads never fall back to disk. "
            "Disk patches require a preview token and create a new output file. "
            "Editor replacement previews are observational; unsaved-buffer apply is unsupported."
        ),
        log_level="WARNING",
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    mutation = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )

    @server.tool(annotations=read_only, structured_output=False)
    def server_status() -> CallToolResult:
        """Report backend availability, configured limits and local path boundaries."""

        def status() -> dict[str, Any]:
            return {
                "name": "010 Editor MCP Bridge",
                "version": __version__,
                "transport": "stdio",
                "standalone": {
                    "available": True,
                    "source": "disk",
                    "capabilities": [
                        "inspect",
                        "read",
                        "search",
                        "diff",
                        "patch_preview",
                        "patch_apply",
                    ],
                },
                "editor": editor.status(),
                "allowed_roots": [str(path) for path in config.allowed_roots],
                "trusted_template_roots": [str(path) for path in config.trusted_template_roots],
                "limits": {
                    name: getattr(config, name)
                    for name in (
                        "max_read_bytes",
                        "max_scan_bytes",
                        "max_results",
                        "chunk_size",
                        "max_patch_bytes",
                        "max_copy_bytes",
                        "max_xml_bytes",
                        "editor_timeout",
                    )
                },
            }

        return _invoke(status)

    @server.tool(annotations=read_only, structured_output=False)
    def editor_status() -> CallToolResult:
        """Detect an existing editor/bridge and report supported live capabilities."""
        return _invoke(editor.status)

    @server.tool(annotations=read_only, structured_output=False)
    def binary_inspect(path: str, hashes: list[str] | None = None) -> CallToolResult:
        """Inspect an allowed ordinary disk file, optionally streaming requested hashes."""
        return _invoke(binary.inspect, path, hashes=hashes)

    @server.tool(annotations=read_only, structured_output=False)
    def binary_read(
        path: str, offset: StrictInt, length: StrictInt, text_preview: StrictBool = True
    ) -> CallToolResult:
        """Read bounded bytes from disk by zero-based byte offset; never read an editor buffer."""
        return _invoke(binary.read, path, offset, length, text_preview=text_preview)

    @server.tool(annotations=read_only, structured_output=False)
    def binary_search(
        path: str,
        pattern_hex: str,
        offset: StrictInt = 0,
        length: StrictInt | None = None,
        max_results: StrictInt = 100,
    ) -> CallToolResult:
        """Search a bounded disk interval for hex bytes, including overlapping matches."""
        return _invoke(binary.search, path, pattern_hex, offset, length, max_results)

    @server.tool(annotations=read_only, structured_output=False)
    def binary_diff(
        path_a: str,
        path_b: str,
        offset: StrictInt = 0,
        length: StrictInt | None = None,
        max_ranges: StrictInt = 100,
    ) -> CallToolResult:
        """Compare bounded disk-file intervals and return precise bounded difference ranges."""
        return _invoke(binary.diff, path_a, path_b, offset, length, max_ranges)

    @server.tool(annotations=read_only, structured_output=False)
    def binary_patch_preview(
        path: str, offset: StrictInt, expected_hex: str, replacement_hex: str
    ) -> CallToolResult:
        """Verify disk bytes and preview replacement; retain its token for a copy-on-write apply."""
        return _invoke(binary.patch_preview, path, offset, expected_hex, replacement_hex)

    @server.tool(annotations=mutation, structured_output=False)
    def binary_patch_apply(
        path: str,
        offset: StrictInt,
        expected_hex: str,
        replacement_hex: str,
        output_path: str,
        preview_token: str,
    ) -> CallToolResult:
        """Apply a previewed disk replacement to a new output file; preserve the original."""
        return _invoke(
            binary.patch_apply,
            path,
            offset,
            expected_hex,
            replacement_hex,
            output_path,
            preview_token,
        )

    @server.tool(annotations=read_only, structured_output=False)
    def editor_context() -> CallToolResult:
        """Read active buffer identity, dirty state and selection without changing selection."""
        return _invoke(editor.context)

    @server.tool(annotations=read_only, structured_output=False)
    def editor_read(buffer_id: str, offset: StrictInt, length: StrictInt) -> CallToolResult:
        """Read the intended live in-memory buffer, including unsaved bytes; no disk fallback."""
        return _invoke(editor.read, buffer_id, offset, length)

    @server.tool(annotations=mutation, structured_output=False)
    def editor_select(buffer_id: str, offset: StrictInt, length: StrictInt) -> CallToolResult:
        """Select/navigate to a zero-based byte range in the explicitly identified live buffer."""
        return _invoke(editor.select, buffer_id, offset, length)

    @server.tool(annotations=mutation, structured_output=False)
    def editor_run_template(buffer_id: str, template_path: str) -> CallToolResult:
        """Run a .bt template from a configured trusted root on the intended live buffer."""
        return _invoke(editor.run_template, buffer_id, template_path)

    @server.tool(annotations=read_only, structured_output=False)
    def editor_patch_preview(
        buffer_id: str, offset: StrictInt, expected_hex: str, replacement_hex: str
    ) -> CallToolResult:
        """Verify and preview live bytes; can_apply is false and no mutation token is issued."""
        return _invoke(editor.patch_preview, buffer_id, offset, expected_hex, replacement_hex)

    @server.tool(annotations=mutation, structured_output=False)
    def editor_patch_apply(
        buffer_id: str,
        offset: StrictInt,
        expected_hex: str,
        replacement_hex: str,
        preview_token: str,
    ) -> CallToolResult:
        """Report CAPABILITY_UNSUPPORTED: atomic unsaved-buffer writes are disabled this release."""
        return _invoke(
            editor.patch_apply, buffer_id, offset, expected_hex, replacement_hex, preview_token
        )

    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="editor010-mcp",
        description="Local stdio MCP server for binary files and an existing 010 Editor session.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--allowed-root", action="append", type=Path, dest="allowed_roots")
    parser.add_argument(
        "--trusted-template-root", action="append", type=Path, dest="trusted_template_roots"
    )
    parser.add_argument("--editor-exe", type=Path)
    parser.add_argument("--editor-pid", type=int)
    parser.add_argument("--bridge-dir", type=Path)
    parser.add_argument("--editor-timeout", type=float)
    parser.add_argument("--preview-ttl-seconds", type=float)
    for name in (
        "max_read_bytes",
        "max_scan_bytes",
        "max_results",
        "chunk_size",
        "max_patch_bytes",
        "max_copy_bytes",
        "max_xml_bytes",
    ):
        parser.add_argument("--" + name.replace("_", "-"), type=int, dest=name)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Launch the stdio server. Stdout is reserved exclusively for MCP messages."""
    args = _parser().parse_args(argv)
    try:
        config = Settings.from_env()
        overrides = {name: value for name, value in vars(args).items() if value is not None}
        for name in ("allowed_roots", "trusted_template_roots"):
            if name in overrides:
                overrides[name] = tuple(overrides[name])
        config = replace(config, **overrides)
        create_server(config).run(transport="stdio")
    except BridgeError as exc:
        print(json.dumps({"error": exc.as_dict()}), file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
