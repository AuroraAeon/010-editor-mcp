"""Correlated, serialized command-line forwarding into one existing editor GUI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import BridgeError, Settings
from .windows import EditorProcess, discover


def _dispatch(exe: Path, script: Path, timeout: float) -> None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    environment = {**os.environ, "EDITOR010_MCP_NEW_PROCESS": "1"}
    try:
        subprocess.run(
            [str(exe), "-nowarnings", f"-script:{script}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
            creationflags=flags,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            "EDITOR_TIMEOUT",
            "Command forwarding timed out; outcome is unknown. "
            "Resolve editor activity and restart the MCP server.",
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BridgeError(
            "BACKEND_UNAVAILABLE", "Could not forward the script to the existing editor."
        ) from exc


@contextmanager
def _process_lock(path: Path):
    with path.open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            try:
                handle.write(b"0")
                handle.flush()
            except OSError as exc:
                raise BridgeError("EDITOR_BUSY", "Another bridge request owns the lock.") from exc
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BridgeError(
                "EDITOR_BUSY", "Another bridge request owns this editor session."
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)


class BridgeTransport:
    def __init__(
        self, settings: Settings, *, detector: Callable = discover, dispatcher: Callable = _dispatch
    ):
        self.settings = settings
        self._detector, self._dispatcher = detector, dispatcher
        self._mutex = threading.Lock()
        self._session: str | None = None
        self._invalid = False
        base = settings.bridge_dir
        if base is None:
            base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "editor010-mcp"
        self.directory = base.resolve()

    def status(self) -> dict[str, Any]:
        try:
            process = self._detect()
        except BridgeError as exc:
            return {
                "source": "editor",
                "available": False,
                "mechanism": "single-instance-cli-script",
                "error": exc.as_dict(),
            }
        return {
            "source": "editor",
            "available": True,
            "mechanism": "single-instance-cli-script",
            "pid": process.pid,
            "session_id": process.identity,
            "editor_version": process.version,
            "session_verified": self._session == process.identity,
            "limitations": [
                "Requires Allow Only One Instance of 010 Editor.",
                "Only the currently active ordinary document is addressed.",
                "No atomic editor patch API; buffer patch apply is unsupported.",
            ],
        }

    def _detect(self) -> EditorProcess:
        if self._invalid:
            raise BridgeError(
                "BACKEND_UNAVAILABLE",
                "Bridge session invalidated after timeout/disconnect. "
                "Resolve editor activity and restart this MCP server.",
            )
        process = self._detector(self.settings)
        if self._session is not None and process.identity != self._session:
            self._invalid = True
            raise BridgeError(
                "EDITOR_DISCONNECTED",
                "The editor session changed. Restart the MCP server to attach again.",
            )
        if process.version != "unknown" and int(process.version.split(".")[0]) < 13:
            raise BridgeError(
                "CAPABILITY_UNSUPPORTED",
                "The bridge requires 010 Editor 13 or newer. Verified version: 16.0.4.",
            )
        return process

    def request(
        self,
        operation: str,
        *,
        index: int = -1,
        name: str = "",
        size: int = -1,
        offset: int = 0,
        length: int = 0,
        template: str = "",
    ) -> tuple[dict, bytes]:
        if operation not in {"context", "read", "select", "template"}:
            raise BridgeError("CAPABILITY_UNSUPPORTED", "Unknown bridge operation.")
        if not self._mutex.acquire(blocking=False):
            raise BridgeError("EDITOR_BUSY", "A bridge request is already in progress.")
        try:
            process = self._detect()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            # PID and creation time isolate lock files across editor restarts.
            with _process_lock(self.directory / f"session-{process.pid}-{process.started}.lock"):
                return self._request(
                    process, operation, index, name, size, offset, length, template
                )
        finally:
            self._mutex.release()

    def _request(
        self,
        process: EditorProcess,
        operation: str,
        index: int,
        name: str,
        size: int,
        offset: int,
        length: int,
        template: str,
    ) -> tuple[dict, bytes]:
        # A unique directory and marker prevent stale/late replies crossing requests.
        with tempfile.TemporaryDirectory(prefix="request-", dir=self.directory) as directory:
            work = Path(directory)
            marker, response, payload = work / "pending", work / "reply.json", work / "payload.bin"
            marker.touch()
            request_id = uuid.uuid4().hex
            values = {
                "REQUEST_ID": request_id,
                "MARKER": marker.as_posix(),
                "RESPONSE": response.as_posix(),
                "PAYLOAD": payload.as_posix(),
                "OPERATION": operation,
                "INDEX": index,
                "NAME": name,
                "SIZE": size,
                "OFFSET": offset,
                "LENGTH": length,
                "TEMPLATE": template,
            }
            resource = Path(__file__).parent / "bridge_scripts" / "command.1sc"
            if not resource.is_file():
                resource = Path(__file__).parents[2] / "bridge" / "scripts" / "command.1sc"
            script = resource.read_text(encoding="utf-8")
            script = re.sub(
                r"@([A-Z_]+)@",
                lambda match: json.dumps(values[match[1]], ensure_ascii=False),
                script,
            )
            script_path = work / "command.1sc"
            script_path.write_text(script, encoding="utf-8")
            deadline = time.monotonic() + self.settings.editor_timeout
            malformed = False
            try:
                self._dispatcher(process.exe, script_path, self.settings.editor_timeout)
                while time.monotonic() < deadline:
                    current = self._detect()
                    if current.identity != process.identity:
                        raise BridgeError(
                            "EDITOR_DISCONNECTED", "Editor session changed during the request."
                        )
                    if response.exists():
                        if response.stat().st_size > 16_384:
                            raise BridgeError(
                                "PROTOCOL_ERROR", "Bridge response exceeds the protocol limit."
                            )
                        try:
                            reply = json.loads(response.read_text(encoding="utf-8"))
                        except (ValueError, UnicodeError):
                            malformed = True
                        else:
                            result = self._validate_reply(reply, request_id)
                            data = b""
                            if operation == "template" or (operation == "read" and length):
                                limit = (
                                    self.settings.max_xml_bytes
                                    if operation == "template"
                                    else length
                                )
                                if not payload.is_file():
                                    raise BridgeError(
                                        "PROTOCOL_ERROR", "Bridge returned no requested payload."
                                    )
                                if payload.stat().st_size > limit:
                                    raise BridgeError(
                                        "LIMIT_EXCEEDED",
                                        "Editor export exceeds the configured result limit.",
                                    )
                                data = payload.read_bytes()
                                if operation == "read" and len(data) != length:
                                    raise BridgeError(
                                        "FILE_CHANGED",
                                        "Live export length changed during the read.",
                                    )
                            self._session = process.identity
                            result["session_id"] = process.identity
                            result["editor_version"] = process.version
                            return result, data
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
                self._invalid = True
                if malformed:
                    raise BridgeError(
                        "PROTOCOL_ERROR", "Bridge returned malformed JSON; session invalidated."
                    )
                raise BridgeError(
                    "EDITOR_TIMEOUT",
                    "No correlated reply. The editor may be busy or the Script/Template failed. "
                    "Outcome is unknown; resolve it and restart this MCP server.",
                )
            except BridgeError as exc:
                if exc.code in {"EDITOR_TIMEOUT", "EDITOR_DISCONNECTED", "PROTOCOL_ERROR"}:
                    self._invalid = True
                raise
            finally:
                marker.unlink(missing_ok=True)

    @staticmethod
    def _validate_reply(reply: Any, request_id: str) -> dict[str, Any]:
        if (
            not isinstance(reply, dict)
            or type(reply.get("protocol")) is not int
            or reply.get("protocol") != 1
            or reply.get("request_id") != request_id
        ):
            raise BridgeError("PROTOCOL_ERROR", "Bridge reply protocol/request ID mismatch.")
        if "error" in reply:
            error = reply["error"]
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)
            ):
                raise BridgeError("PROTOCOL_ERROR", "Invalid error envelope.")
            raise BridgeError(error["code"], error["message"])
        result = reply.get("result")
        if not isinstance(result, dict):
            raise BridgeError("PROTOCOL_ERROR", "Missing bridge result.")
        for key in (
            "index",
            "size",
            "dirty",
            "read_only",
            "cursor_offset",
            "display_start_address",
        ):
            if type(result.get(key)) is not int:
                raise BridgeError("PROTOCOL_ERROR", "Invalid context integer fields.")
        selection = result.get("selection")
        if (
            result["index"] < 0
            or result["size"] < 0
            or not isinstance(result.get("path"), str)
            or not isinstance(selection, dict)
            or any(type(selection.get(key)) is not int for key in ("offset", "length"))
            or result["dirty"] not in (0, 1)
            or result["read_only"] not in (0, 1)
        ):
            raise BridgeError("PROTOCOL_ERROR", "Invalid editor context fields.")
        if (
            selection["offset"] < 0
            or selection["length"] < 0
            or selection["offset"] > result["size"]
            or selection["length"] > result["size"] - selection["offset"]
            or not 0 <= result["cursor_offset"] <= result["size"]
        ):
            raise BridgeError("PROTOCOL_ERROR", "Invalid live selection or cursor bounds.")
        return result
