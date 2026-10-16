"""Live-only editor operations. Filesystem data is never a buffer-read fallback."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any

from .bridge_protocol import BridgeTransport
from .models import BridgeError, Settings, parse_hex, resolve_allowed_path, validate_range


class EditorBackend:
    def __init__(self, settings: Settings, transport: BridgeTransport | None = None):
        self.settings = settings
        self.transport = transport or BridgeTransport(settings)
        self._contexts: dict[str, tuple[float, dict]] = {}
        self._mutex = threading.Lock()

    def status(self) -> dict[str, Any]:
        result = self.transport.status()
        available = result["available"]
        result["capabilities"] = {
            name: available
            for name in ("context", "read", "select", "run_template", "patch_preview")
        }
        result["capabilities"]["patch_apply"] = False
        result["patch_apply_reason"] = (
            "010 Script runs concurrently with user edits and has no documented atomic "
            "expected-byte replacement or buffer lifetime ID. Apply is disabled."
        )
        result["verified_versions"] = ["16.0.4"]
        return result

    def _remember(self, result: dict) -> dict:
        now = time.monotonic()
        self._contexts = {key: value for key, value in self._contexts.items() if value[0] > now}
        # Bound retained identities even when a client repeatedly requests context.
        if len(self._contexts) >= self.settings.max_results:
            self._contexts.pop(next(iter(self._contexts)))
        token = uuid.uuid4().hex
        self._contexts[token] = (now + self.settings.preview_ttl_seconds, result.copy())
        return self._context_result(result, token)

    @staticmethod
    def _context_result(result: dict, token: str) -> dict:
        return {
            **result,
            "source": "editor",
            "buffer_id": token,
            "dirty": bool(result["dirty"]),
            "read_only": bool(result["read_only"]),
            "identity_scope": (
                "session + active index + filename + size; same-path reopen cannot be detected"
            ),
            "consistency": "observed live state; concurrent user edits are possible",
        }

    def context(self) -> dict[str, Any]:
        with self._mutex:
            result, _ = self.transport.request("context")
            return self._remember(result)

    def _target(self, buffer_id: str) -> dict:
        availability = self.transport.status()
        if not availability["available"]:
            error = availability.get("error", {})
            raise BridgeError(
                error.get("code", "BACKEND_UNAVAILABLE"),
                error.get("message", "The editor bridge is unavailable."),
            )
        found = self._contexts.get(buffer_id)
        if found is None or found[0] <= time.monotonic():
            raise BridgeError(
                "STALE_CONTEXT", "Unknown or expired buffer_id; call editor_context first."
            )
        result = found[1]
        # This is path-policy validation, never a source-byte read. New/untitled
        # buffers have no reliable filesystem identity and are not addressable yet.
        from pathlib import Path

        if not Path(result["path"]).is_absolute():
            raise BridgeError(
                "CAPABILITY_UNSUPPORTED",
                "Save a name for this untitled buffer before addressing it through the bridge.",
            )
        resolve_allowed_path(result["path"], self.settings.allowed_roots, must_exist=False)
        # The bridge script validates these against the current active document.
        return {"index": result["index"], "name": result["path"], "size": result["size"]}

    def _call(self, operation: str, buffer_id: str, **kwargs) -> tuple[dict, bytes]:
        target = self._target(buffer_id)
        result, payload = self.transport.request(operation, **target, **kwargs)
        previous = self._contexts[buffer_id][1]
        if any(result[key] != previous[key] for key in ("session_id", "index", "path", "size")):
            raise BridgeError("STALE_CONTEXT", "The editor target changed; request a new context.")
        resolve_allowed_path(result["path"], self.settings.allowed_roots, must_exist=False)
        return self._context_result(result, buffer_id), payload

    def read(self, buffer_id: str, offset: int, length: int) -> dict[str, Any]:
        with self._mutex:
            target = self._target(buffer_id)
            validate_range(offset, length, target["size"], self.settings.max_read_bytes)
            result, data = self._call("read", buffer_id, offset=offset, length=length)
            return {
                **result,
                "offset": offset,
                "length": len(data),
                "hex": data.hex(),
                "text_preview": "".join(chr(b) if 32 <= b < 127 else "." for b in data),
            }

    def select(self, buffer_id: str, offset: int, length: int) -> dict[str, Any]:
        with self._mutex:
            target = self._target(buffer_id)
            validate_range(offset, length, target["size"])
            if length > 2_147_483_647:
                raise BridgeError(
                    "LIMIT_EXCEEDED", "Selection length exceeds the Script bridge limit."
                )
            result, _ = self._call("select", buffer_id, offset=offset, length=length)
            return {**result, "offset": offset, "length": length}

    def run_template(self, buffer_id: str, template_path: str) -> dict[str, Any]:
        if not self.settings.trusted_template_roots:
            raise BridgeError(
                "CAPABILITY_UNSUPPORTED",
                "Configure trusted_template_roots before running a template.",
            )
        path = resolve_allowed_path(template_path, self.settings.trusted_template_roots)
        if path.suffix.lower() != ".bt":
            raise BridgeError("INVALID_ARGUMENT", "Only trusted .bt Binary Templates may run.")
        if path.stat().st_size > self.settings.max_read_bytes:
            raise BridgeError("LIMIT_EXCEEDED", "Trusted template exceeds max_read_bytes.")
        template_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._mutex:
            result, data = self._call("template", buffer_id, template=path.as_posix())
            try:
                text = data.decode("utf-8-sig")
                if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                    raise BridgeError("PROTOCOL_ERROR", "DTD/entity XML exports are not supported.")
                root = ET.fromstring(text)
            except (ET.ParseError, ValueError, UnicodeError) as exc:
                raise BridgeError("PROTOCOL_ERROR", "Template returned invalid XML.") from exc
            if (
                path.stat().st_size > self.settings.max_read_bytes
                or hashlib.sha256(path.read_bytes()).hexdigest() != template_digest
            ):
                raise BridgeError("FILE_CHANGED", "Trusted template changed during execution.")
            variables = []
            for node in root.iter("variable"):
                if len(variables) == self.settings.max_results:
                    break
                entry = {
                    key: node.findtext(key) for key in ("name", "type", "value", "start", "size")
                }
                for key, source in (("offset", "start"), ("length", "size")):
                    raw = entry[source] or ""
                    try:
                        number = int(raw[:-1], 16) if raw.lower().endswith("h") else int(raw)
                        entry[key] = (
                            number - result["display_start_address"] if key == "offset" else number
                        )
                    except ValueError:
                        entry[key] = None
                variables.append(entry)
            return {
                **result,
                "template_path": str(path),
                "template_sha256": template_digest,
                "xml": text,
                "xml_root": root.tag,
                "xml_bytes": len(data),
                "variables": variables,
                "variables_truncated": len(list(root.iter("variable"))) > len(variables),
                "xml_coordinates": (
                    "Raw XML start fields are display addresses. "
                    "variables[].offset is normalized to zero-based file-local bytes."
                ),
                "result_scope": (
                    "Expanded materialized results; open=suppress or lazy variables may be omitted."
                ),
            }

    def patch_preview(
        self, buffer_id: str, offset: int, expected_hex: str, replacement_hex: str
    ) -> dict[str, Any]:
        expected = parse_hex(expected_hex, "expected_hex", self.settings.max_patch_bytes)
        replacement = parse_hex(replacement_hex, "replacement_hex", self.settings.max_patch_bytes)
        if not expected or len(expected) != len(replacement):
            raise BridgeError(
                "INVALID_ARGUMENT", "Replacement must be nonempty and preserve byte length."
            )
        result = self.read(buffer_id, offset, len(expected))
        if bytes.fromhex(result["hex"]) != expected:
            raise BridgeError("PRECONDITION_FAILED", "Live bytes do not match expected_hex.")
        return {
            **result,
            "before_hex": expected.hex(),
            "after_hex": replacement.hex(),
            "can_apply": False,
            "apply_error": "CAPABILITY_UNSUPPORTED",
            "reason": (
                "No proven atomic expected-byte replacement in 010 Editor. "
                "Preview is observational; no editor bytes were modified."
            ),
        }

    def patch_apply(
        self,
        buffer_id: str,
        offset: int,
        expected_hex: str,
        replacement_hex: str,
        preview_token: str,
    ) -> dict[str, Any]:
        raise BridgeError(
            "CAPABILITY_UNSUPPORTED",
            "Unsaved-buffer apply is disabled: 010 Script permits concurrent user edits "
            "and exposes no proven atomic expected-byte mutation. "
            "Review the preview and edit manually in 010 Editor.",
        )
