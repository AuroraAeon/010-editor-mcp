"""Bounded, portable binary operations; the editor is deliberately not a dependency."""

from __future__ import annotations

import functools
import hashlib
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

from .models import (
    BridgeError,
    Settings,
    file_identity,
    parse_hex,
    resolve_allowed_path,
    validate_range,
)

_F = TypeVar("_F", bound=Callable[..., dict[str, Any]])


def _io_errors(method: _F) -> _F:
    @functools.wraps(method)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return method(*args, **kwargs)
        except FileNotFoundError as exc:
            raise BridgeError("PATH_NOT_FOUND", "File or parent directory does not exist.") from exc
        except OSError as exc:
            raise BridgeError(
                "IO_ERROR", "The local file operation failed.", {"operation": method.__name__}
            ) from exc

    return wrapped  # type: ignore[return-value]


@dataclass(frozen=True)
class _Preview:
    path: Path
    file_id: str
    offset: int
    expected: bytes
    replacement: bytes
    expires: float


class BinaryEngine:
    """Operate only on ordinary files within explicitly configured roots.

    Patch receipts are local to this server process and expire. Copy-on-write
    checks the source before and after streaming and removes an incomplete
    output on failure. Like ordinary file tools, it cannot lock out unrelated
    applications that ignore file locks; concurrent changes are conflicts.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._previews: dict[str, _Preview] = {}
        self._preview_lock = threading.RLock()

    def _path(self, path: str | Path, *, must_exist: bool = True) -> Path:
        return resolve_allowed_path(path, self.settings.allowed_roots, must_exist=must_exist)

    @staticmethod
    def _metadata(path: Path, stat: os.stat_result) -> dict[str, Any]:
        return {
            "source": "disk",
            "path": str(path),
            "file_id": file_identity(path, stat),
            "size": stat.st_size,
        }

    def _scan_range(self, offset: int, length: int | None, size: int) -> int:
        # Validate offset before subtraction; bool is not an integer offset.
        validate_range(offset, 0, size)
        length = size - offset if length is None else length
        validate_range(offset, length, size, self.settings.max_scan_bytes)
        return length

    def _result_limit(self, count: int) -> int:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise BridgeError("INVALID_ARGUMENT", "Result limit must be a positive integer.")
        if count > self.settings.max_results:
            raise BridgeError(
                "LIMIT_EXCEEDED",
                "Result limit exceeds the configured maximum.",
                {"limit": self.settings.max_results},
            )
        return count

    @staticmethod
    def _unchanged(path: Path, handle: BinaryIO, file_id: str) -> None:
        # Both the handle and pathname must still identify the previewed file.
        if (
            file_identity(path, os.fstat(handle.fileno())) != file_id
            or file_identity(path) != file_id
        ):
            raise BridgeError(
                "FILE_CHANGED", "The source changed during the operation; retry after reviewing it."
            )

    @staticmethod
    def _read_exact(handle: BinaryIO, length: int) -> bytes:
        value = handle.read(length)
        if len(value) != length:
            raise BridgeError("FILE_CHANGED", "The source became shorter during the operation.")
        return value

    @_io_errors
    def inspect(self, path: str | Path, hashes: list[str] | None = None) -> dict[str, Any]:
        path = self._path(path)
        supported = {"md5", "sha1", "sha256", "sha512", "blake2b", "blake2s"}
        if hashes is not None and (
            not isinstance(hashes, list)
            or any(not isinstance(name, str) or name not in supported for name in hashes)
        ):
            raise BridgeError(
                "INVALID_ARGUMENT", "Unsupported hash algorithm.", {"supported": sorted(supported)}
            )
        stat = path.stat()
        result = {**self._metadata(path, stat), "modified_time_ns": stat.st_mtime_ns, "hashes": {}}
        if not hashes:
            return result
        validate_range(0, stat.st_size, stat.st_size, self.settings.max_scan_bytes)
        algorithms = {
            name: hashlib.new(name, usedforsecurity=False) for name in dict.fromkeys(hashes)
        }
        with path.open("rb") as handle:
            self._unchanged(path, handle, result["file_id"])
            remaining = stat.st_size
            while remaining:
                chunk = self._read_exact(handle, min(remaining, self.settings.chunk_size))
                remaining -= len(chunk)
                for algorithm in algorithms.values():
                    algorithm.update(chunk)
            self._unchanged(path, handle, result["file_id"])
        result["hashes"] = {name: algorithm.hexdigest() for name, algorithm in algorithms.items()}
        return result

    @_io_errors
    def read(
        self, path: str | Path, offset: int, length: int, text_preview: bool = True
    ) -> dict[str, Any]:
        path = self._path(path)
        if not isinstance(text_preview, bool):
            raise BridgeError("INVALID_ARGUMENT", "text_preview must be a boolean.")
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            validate_range(offset, length, stat.st_size, self.settings.max_read_bytes)
            result = self._metadata(path, stat)
            handle.seek(offset)
            data = self._read_exact(handle, length)
            self._unchanged(path, handle, result["file_id"])
        result.update(offset=offset, length=length, hex=data.hex())
        if text_preview:
            result["text_preview"] = "".join(
                chr(byte) if 32 <= byte < 127 else "." for byte in data
            )
            result["text_encoding"] = "printable-ascii"
        return result

    @_io_errors
    def search(
        self,
        path: str | Path,
        pattern_hex: str,
        offset: int = 0,
        length: int | None = None,
        max_results: int = 100,
    ) -> dict[str, Any]:
        path = self._path(path)
        pattern = parse_hex(pattern_hex, "pattern_hex", self.settings.max_read_bytes)
        if not pattern:
            raise BridgeError("INVALID_ARGUMENT", "Search pattern must contain at least one byte.")
        max_results = self._result_limit(max_results)
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            length = self._scan_range(offset, length, stat.st_size)
            result = self._metadata(path, stat)
            matches: list[dict[str, int]] = []
            handle.seek(offset)
            remaining, scanned = length, 0
            tail = b""
            truncated = False
            while remaining:
                chunk = self._read_exact(handle, min(remaining, self.settings.chunk_size))
                base = offset + scanned - len(tail)
                data = tail + chunk
                scanned += len(chunk)
                remaining -= len(chunk)
                cursor = 0
                while (index := data.find(pattern, cursor)) != -1:
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    matches.append({"offset": base + index, "length": len(pattern)})
                    cursor = index + 1  # Overlapping occurrences are meaningful.
                if truncated:
                    break
                tail = data[-(len(pattern) - 1) :] if len(pattern) > 1 else b""
            self._unchanged(path, handle, result["file_id"])
        return {
            **result,
            "offset": offset,
            "length": length,
            "pattern_hex": pattern.hex(),
            "matches": matches,
            "truncated": truncated,
            "bytes_scanned": scanned,
        }

    @_io_errors
    def diff(
        self,
        path_a: str | Path,
        path_b: str | Path,
        offset: int = 0,
        length: int | None = None,
        max_ranges: int = 100,
    ) -> dict[str, Any]:
        path_a, path_b = self._path(path_a), self._path(path_b)
        max_ranges = self._result_limit(max_ranges)
        # Bound byte previews in aggregate, independently of input file size.
        preview_size = min(32, self.settings.max_read_bytes // (max_ranges * 2))
        with path_a.open("rb") as left, path_b.open("rb") as right:
            stat_a, stat_b = os.fstat(left.fileno()), os.fstat(right.fileno())
            id_a, id_b = file_identity(path_a, stat_a), file_identity(path_b, stat_b)
            size = max(stat_a.st_size, stat_b.st_size)
            length = self._scan_range(offset, length, size)
            left.seek(offset)
            right.seek(offset)
            ranges: list[dict[str, Any]] = []
            active: dict[str, Any] | None = None
            before, after = bytearray(), bytearray()
            scanned = 0
            truncated = False

            def finish_range() -> None:
                nonlocal active
                if active is not None:
                    active.update(
                        a_hex=before.hex(),
                        b_hex=after.hex(),
                        preview_truncated=active["a_length"] > len(before)
                        or active["b_length"] > len(after),
                    )
                    ranges.append(active)
                    active = None
                    before.clear()
                    after.clear()

            while scanned < length and not truncated:
                count = min(length - scanned, self.settings.chunk_size)
                start = offset + scanned
                a = self._read_exact(left, min(count, max(0, stat_a.st_size - start)))
                b = self._read_exact(right, min(count, max(0, stat_b.st_size - start)))
                if a == b:
                    finish_range()
                    scanned += count
                    continue
                for index in range(count):
                    a_byte = a[index] if index < len(a) else None
                    b_byte = b[index] if index < len(b) else None
                    if a_byte == b_byte:
                        finish_range()
                    else:
                        if active is None:
                            if len(ranges) >= max_ranges:
                                truncated = True
                                break
                            active = {
                                "offset": start + index,
                                "length": 0,
                                "a_length": 0,
                                "b_length": 0,
                            }
                        active["length"] += 1
                        if a_byte is not None:
                            active["a_length"] += 1
                            if len(before) < preview_size:
                                before.append(a_byte)
                        if b_byte is not None:
                            active["b_length"] += 1
                            if len(after) < preview_size:
                                after.append(b_byte)
                    scanned += 1
            finish_range()
            self._unchanged(path_a, left, id_a)
            self._unchanged(path_b, right, id_b)
        return {
            "source": "disk",
            "path_a": str(path_a),
            "path_b": str(path_b),
            "file_id_a": id_a,
            "file_id_b": id_b,
            "size_a": stat_a.st_size,
            "size_b": stat_b.st_size,
            "offset": offset,
            "length": length,
            "ranges": ranges,
            "truncated": truncated,
            "bytes_scanned": scanned,
        }

    def _patch_bytes(self, expected_hex: str, replacement_hex: str) -> tuple[bytes, bytes]:
        expected = parse_hex(expected_hex, "expected_hex", self.settings.max_patch_bytes)
        replacement = parse_hex(replacement_hex, "replacement_hex", self.settings.max_patch_bytes)
        if not expected or len(expected) != len(replacement):
            raise BridgeError(
                "INVALID_ARGUMENT",
                "Patch must replace a nonempty range with the same number of bytes.",
            )
        return expected, replacement

    def _prune_previews(self) -> None:
        now = time.monotonic()
        for token in [token for token, preview in self._previews.items() if preview.expires <= now]:
            del self._previews[token]

    @_io_errors
    def patch_preview(
        self, path: str | Path, offset: int, expected_hex: str, replacement_hex: str
    ) -> dict[str, Any]:
        path = self._path(path)
        expected, replacement = self._patch_bytes(expected_hex, replacement_hex)
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            validate_range(offset, len(expected), stat.st_size)
            result = self._metadata(path, stat)
            handle.seek(offset)
            actual = self._read_exact(handle, len(expected))
            if actual != expected:
                raise BridgeError(
                    "PRECONDITION_FAILED",
                    "Source bytes do not match expected_hex.",
                    {"offset": offset, "length": len(expected)},
                )
            self._unchanged(path, handle, result["file_id"])
        with self._preview_lock:
            self._prune_previews()
            if len(self._previews) >= min(128, self.settings.max_results):
                raise BridgeError(
                    "LIMIT_EXCEEDED",
                    "Too many outstanding patch previews; apply one or wait for expiry.",
                )
            token = secrets.token_urlsafe(32)
            self._previews[token] = _Preview(
                path,
                result["file_id"],
                offset,
                expected,
                replacement,
                time.monotonic() + self.settings.preview_ttl_seconds,
            )
        return {
            **result,
            "offset": offset,
            "length": len(expected),
            "before_hex": expected.hex(),
            "after_hex": replacement.hex(),
            "preview_token": token,
            "expires_in_seconds": self.settings.preview_ttl_seconds,
            "write_mode": "new-output-only",
        }

    @_io_errors
    def patch_apply(
        self,
        path: str | Path,
        offset: int,
        expected_hex: str,
        replacement_hex: str,
        output_path: str | Path,
        preview_token: str,
    ) -> dict[str, Any]:
        path = self._path(path)
        output = self._path(output_path, must_exist=False)
        expected, replacement = self._patch_bytes(expected_hex, replacement_hex)
        if output == path:
            raise BridgeError(
                "CAPABILITY_UNSUPPORTED",
                "In-place writes are not supported. Choose a new output path.",
            )
        if not isinstance(preview_token, str):
            raise BridgeError("PREVIEW_REQUIRED", "Pass the token from binary_patch_preview.")
        # Serialize apply to guarantee a preview can produce only one output.
        with self._preview_lock:
            preview = self._previews.get(preview_token)
            if preview is None:
                raise BridgeError(
                    "PREVIEW_REQUIRED",
                    "Unknown or already used preview token; preview the patch first.",
                )
            if preview.expires <= time.monotonic():
                del self._previews[preview_token]
                raise BridgeError("STALE_PREVIEW", "The preview expired; preview the patch again.")
            if (path, offset, expected, replacement) != (
                preview.path,
                preview.offset,
                preview.expected,
                preview.replacement,
            ):
                raise BridgeError("PRECONDITION_FAILED", "Patch arguments differ from the preview.")
            created = False
            try:
                with path.open("rb") as source:
                    stat = os.fstat(source.fileno())
                    validate_range(offset, len(expected), stat.st_size)
                    if stat.st_size > self.settings.max_copy_bytes:
                        raise BridgeError(
                            "LIMIT_EXCEEDED",
                            "Source exceeds the configured copy limit.",
                            {"limit": self.settings.max_copy_bytes},
                        )
                    if file_identity(path, stat) != preview.file_id:
                        raise BridgeError(
                            "STALE_PREVIEW",
                            "Source identity changed after preview; preview it again.",
                        )
                    self._unchanged(path, source, preview.file_id)
                    source.seek(offset)
                    if self._read_exact(source, len(expected)) != expected:
                        raise BridgeError(
                            "PRECONDITION_FAILED", "Source bytes changed after preview."
                        )
                    source.seek(0)
                    try:
                        destination = output.open("xb")
                    except FileExistsError as exc:
                        raise BridgeError(
                            "OUTPUT_EXISTS", "Output already exists; choose a new output path."
                        ) from exc
                    created = True
                    with destination:
                        position = 0
                        while position < stat.st_size:
                            chunk = self._read_exact(
                                source, min(self.settings.chunk_size, stat.st_size - position)
                            )
                            end = position + len(chunk)
                            patch_start, patch_end = (
                                max(offset, position),
                                min(offset + len(expected), end),
                            )
                            if patch_start < patch_end:
                                local_start, local_end = (
                                    patch_start - position,
                                    patch_end - position,
                                )
                                expected_start, expected_end = (
                                    patch_start - offset,
                                    patch_end - offset,
                                )
                                if (
                                    chunk[local_start:local_end]
                                    != expected[expected_start:expected_end]
                                ):
                                    raise BridgeError(
                                        "PRECONDITION_FAILED",
                                        "Source bytes changed during copying.",
                                    )
                                chunk = (
                                    chunk[:local_start]
                                    + replacement[expected_start:expected_end]
                                    + chunk[local_end:]
                                )
                            destination.write(chunk)
                            position = end
                        destination.flush()
                        os.fsync(destination.fileno())
                        self._unchanged(path, source, preview.file_id)
                del self._previews[preview_token]
                return {
                    "source": "disk",
                    "path": str(path),
                    "source_file_id": preview.file_id,
                    "output_path": str(output),
                    "output_file_id": file_identity(output),
                    "size": stat.st_size,
                    "offset": offset,
                    "length": len(expected),
                    "before_hex": expected.hex(),
                    "after_hex": replacement.hex(),
                    "original_unchanged": True,
                    "write_mode": "new-output-only",
                }
            except BaseException:
                if created:
                    output.unlink(missing_ok=True)
                raise
