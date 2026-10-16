"""Shared limits, path policy and machine-readable errors (no editor dependency)."""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BridgeError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass
class Settings:
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: (Path.cwd().resolve(),))
    trusted_template_roots: tuple[Path, ...] = ()
    max_read_bytes: int = 65_536
    max_scan_bytes: int = 268_435_456
    max_results: int = 1000
    chunk_size: int = 1_048_576
    max_patch_bytes: int = 65_536
    max_copy_bytes: int = 1_073_741_824
    max_xml_bytes: int = 1_048_576
    preview_ttl_seconds: float = 300
    editor_exe: Path | None = None
    editor_pid: int | None = None
    editor_timeout: float = 15
    bridge_dir: Path | None = None

    def __post_init__(self) -> None:
        try:
            self.allowed_roots = tuple(Path(p).expanduser().resolve() for p in self.allowed_roots)
            self.trusted_template_roots = tuple(
                Path(p).expanduser().resolve() for p in self.trusted_template_roots
            )
            if self.editor_exe is not None:
                self.editor_exe = Path(self.editor_exe).expanduser().resolve()
            if self.bridge_dir is not None:
                self.bridge_dir = Path(self.bridge_dir).expanduser().resolve()
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise BridgeError("INVALID_ARGUMENT", "Invalid configured local path.") from exc
        if not self.allowed_roots:
            raise BridgeError("INVALID_ARGUMENT", "Configure at least one allowed root.")
        for name in (
            "max_read_bytes",
            "max_scan_bytes",
            "max_results",
            "chunk_size",
            "max_patch_bytes",
            "max_copy_bytes",
            "max_xml_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise BridgeError("INVALID_ARGUMENT", f"{name} must be a positive integer.")
        for name in ("preview_ttl_seconds", "editor_timeout"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise BridgeError("INVALID_ARGUMENT", f"{name} must be a finite positive number.")
        if self.editor_pid is not None and (
            isinstance(self.editor_pid, bool)
            or not isinstance(self.editor_pid, int)
            or self.editor_pid <= 0
        ):
            raise BridgeError("INVALID_ARGUMENT", "editor_pid must be a positive integer.")

    @classmethod
    def from_env(cls) -> Settings:
        prefix = "EDITOR010_"
        args: dict[str, Any] = {}
        for key in ("allowed_roots", "trusted_template_roots"):
            if raw := os.environ.get(prefix + key.upper()):
                args[key] = tuple(Path(p) for p in raw.split(os.pathsep) if p)
        for key in ("editor_exe", "bridge_dir"):
            if raw := os.environ.get(prefix + key.upper()):
                args[key] = Path(raw)
        try:
            for key in (
                "max_read_bytes",
                "max_scan_bytes",
                "max_results",
                "chunk_size",
                "max_patch_bytes",
                "max_copy_bytes",
                "max_xml_bytes",
                "editor_pid",
            ):
                if raw := os.environ.get(prefix + key.upper()):
                    args[key] = int(raw)
            for key in ("editor_timeout", "preview_ttl_seconds"):
                if raw := os.environ.get(prefix + key.upper()):
                    args[key] = float(raw)
        except ValueError as exc:
            raise BridgeError(
                "INVALID_ARGUMENT", "Invalid numeric EDITOR010 configuration."
            ) from exc
        return cls(**args)


def parse_hex(value: str, name: str = "hex", max_bytes: int | None = None) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F\s]*", value) is None:
        raise BridgeError("INVALID_ARGUMENT", f"{name} must contain hexadecimal byte pairs.")
    compact = "".join(value.split())
    if len(compact) % 2:
        raise BridgeError("INVALID_ARGUMENT", f"{name} must contain complete byte pairs.")
    if max_bytes is not None and len(compact) // 2 > max_bytes:
        raise BridgeError("LIMIT_EXCEEDED", f"{name} exceeds the byte limit.")
    return bytes.fromhex(compact)


def validate_range(offset: int, length: int, size: int, max_length: int | None = None) -> None:
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (offset, length, size)):
        raise BridgeError(
            "INVALID_ARGUMENT", "offset, length and size must be nonnegative integers."
        )
    if offset > size or length > size - offset:
        raise BridgeError("OUT_OF_BOUNDS", "Byte range exceeds the file or buffer.", {"size": size})
    if max_length is not None and length > max_length:
        raise BridgeError(
            "LIMIT_EXCEEDED",
            "Requested byte range exceeds the configured limit.",
            {"limit": max_length},
        )


def resolve_allowed_path(
    path: str | Path, roots: tuple[Path, ...], must_exist: bool = True
) -> Path:
    if not isinstance(path, (str, Path)):
        raise BridgeError("INVALID_ARGUMENT", "A local file path must be a string or Path.")
    raw = str(path)
    # Exclude Windows devices, alternate streams and network paths even on POSIX.
    absolute_drive = bool(re.match(r"^[a-zA-Z]:[/\\]", raw))
    colon_forbidden = ":" in (raw[2:] if absolute_drive else raw)
    if (
        not raw.strip()
        or raw.replace("\\", "/").startswith("//")
        or colon_forbidden
        or "\x00" in raw
    ):
        raise BridgeError(
            "PATH_NOT_ALLOWED", "Only ordinary local files inside allowed roots are supported."
        )
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in "123456789¹²³"),
        *(f"LPT{i}" for i in "123456789¹²³"),
    }
    if any(
        part.rstrip(" .").split(".")[0].rstrip().upper() in reserved
        for part in re.split(r"[/\\]", raw)
    ):
        raise BridgeError("PATH_NOT_ALLOWED", "Device paths are not allowed.")
    try:
        # Check the root before existence so missing out-of-root paths are still
        # policy errors. resolve() also prevents an ordinary symlink escaping.
        target = Path(path).expanduser().resolve(strict=False)
        if not any(target.is_relative_to(Path(root).resolve()) for root in roots):
            raise BridgeError("PATH_NOT_ALLOWED", "Path is outside configured allowed roots.")
        if target.exists() and not target.is_file():
            raise BridgeError("PATH_NOT_ALLOWED", "Target must be an ordinary file.")
        if must_exist and not target.is_file():
            raise BridgeError("PATH_NOT_FOUND", "File does not exist.")
        if not target.parent.is_dir():
            raise BridgeError("PATH_NOT_FOUND", "Parent directory does not exist.")
        return target
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise BridgeError("PATH_NOT_FOUND", "File or parent directory does not exist.") from exc
    except OSError as exc:
        raise BridgeError("IO_ERROR", "Cannot resolve file path.") from exc
    except (ValueError, RuntimeError) as exc:
        raise BridgeError("PATH_NOT_ALLOWED", "Invalid or cyclic local path.") from exc


def file_identity(path: Path, stat: os.stat_result | None = None) -> str:
    stat = stat or path.stat()
    # On Windows, some Python/filesystem combinations expose birth time in
    # stat().st_ctime and change time in fstat().st_ctime. They cannot be mixed.
    ctime = stat.st_ctime_ns if os.name != "nt" else 0
    value = (
        f"{path.resolve()}|{stat.st_dev}|{stat.st_ino}|{stat.st_size}|{stat.st_mtime_ns}|{ctime}"
    )
    return hashlib.sha256(value.encode()).hexdigest()
