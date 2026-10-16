"""Contract tests use an injected dispatcher, never a pretend live GUI.

These tests prove request correlation, bounded exports and failure behavior.
Actual 010 Script semantics are covered separately by opt-in live-editor tests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from editor010_mcp.bridge_protocol import BridgeTransport
from editor010_mcp.editor_backend import EditorBackend
from editor010_mcp.models import BridgeError, Settings
from editor010_mcp.windows import EditorProcess


class Harness:
    def __init__(self, tmp_path):
        self.document = tmp_path / "document.bin"
        self.document.write_bytes(b"DISK000")
        self.templates = tmp_path / "trusted"
        self.templates.mkdir()
        self.template = self.templates / "sample.bt"
        self.template.write_text("uchar value;", encoding="utf-8")
        self.settings = Settings(
            allowed_roots=(tmp_path,),
            trusted_template_roots=(self.templates,),
            bridge_dir=tmp_path / "ipc",
            editor_timeout=0.08,
        )
        self.process = EditorProcess(101, 202, tmp_path / "010editor.exe", 303, "16.0.4")
        self.live = b"\x00LIVE\xff!"
        self.context = {
            "index": 2,
            "path": str(self.document),
            "size": len(self.live),
            "dirty": 1,
            "read_only": 0,
            "selection": {"offset": 1, "length": 2},
            "cursor_offset": 1,
            "display_start_address": 4096,
        }
        self.xml = b'<?xml version="1.0" encoding="UTF-8"?><results><value>7</value></results>'
        self.calls = []
        self.workdirs = []
        self.marker_paths = []
        self.on_dispatch = None
        self.mutate_reply = None
        self.raw_reply = None
        self.emit_reply = True
        self.emit_payload = True
        self.payload_override = None
        self.failure = None
        self.detect_error = None
        self.transport = BridgeTransport(
            self.settings, detector=self.detect, dispatcher=self.dispatch
        )
        self.backend = EditorBackend(self.settings, self.transport)

    def detect(self, _settings):
        if self.detect_error:
            raise self.detect_error
        return self.process

    def dispatch(self, exe, script, timeout):
        assert exe == self.process.exe
        assert timeout == self.settings.editor_timeout
        content = script.read_text(encoding="utf-8")
        names = (
            "requestId",
            "markerPath",
            "responsePath",
            "payloadPath",
            "operation",
            "expectedIndex",
            "expectedName",
            "expectedSize",
            "offset",
            "length",
            "templatePath",
        )
        values = {}
        for name in names:
            found = re.search(rf"^(?:string|int(?:64)?) {name} = (.+);$", content, re.M)
            assert found is not None, f"Missing rendered declaration: {name}"
            values[name] = json.loads(found[1])
        self.calls.append(values)
        self.workdirs.append(script.parent)
        self.marker_paths.append(Path(values["markerPath"]))
        assert self.marker_paths[-1].is_file()
        if self.on_dispatch:
            self.on_dispatch(values)
        if self.failure:
            raise self.failure
        result = copy.deepcopy(self.context)
        if values["operation"] == "select":
            result["selection"] = {"offset": values["offset"], "length": values["length"]}
        reply = {"protocol": 1, "request_id": values["requestId"], "result": result}
        if self.mutate_reply:
            self.mutate_reply(reply)
        if self.emit_payload:
            if values["operation"] == "read" and values["length"]:
                data = self.live[values["offset"] : values["offset"] + values["length"]]
            elif values["operation"] == "template":
                data = self.xml
            else:
                data = None
            if data is not None:
                if self.payload_override is not None:
                    data = self.payload_override
                Path(values["payloadPath"]).write_bytes(data)
        if self.emit_reply:
            data = json.dumps(reply).encode() if self.raw_reply is None else self.raw_reply
            Path(values["responsePath"]).write_bytes(data)


@pytest.fixture
def bridge(tmp_path):
    return Harness(tmp_path)


def fails(code, operation, *args, **kwargs):
    with pytest.raises(BridgeError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def clean_requests(bridge):
    assert bridge.workdirs
    assert all(not directory.exists() for directory in bridge.workdirs)
    assert all(not marker.exists() for marker in bridge.marker_paths)


def test_status_discovery_is_read_only_and_initially_unverified(bridge):
    status = bridge.transport.status()
    assert status["available"] is True
    assert status["source"] == "editor"
    assert status["session_id"] == "101:202"
    assert status["session_verified"] is False
    assert bridge.calls == []


def test_successful_correlated_context_cleans_owned_temp_files(bridge):
    result, payload = bridge.transport.request("context")
    assert result["session_id"] == bridge.process.identity
    assert result["editor_version"] == "16.0.4"
    assert payload == b""
    assert bridge.transport.status()["session_verified"] is True
    clean_requests(bridge)
    assert list(bridge.transport.directory.glob("*.lock"))


def test_read_payload_is_live_bytes_and_never_disk_fallback(bridge):
    context = bridge.backend.context()
    result = bridge.backend.read(context["buffer_id"], 1, 5)
    assert result["hex"] == b"LIVE\xff".hex()
    assert result["text_preview"] == "LIVE."
    assert result["source"] == "editor" and result["dirty"] is True
    assert bridge.document.read_bytes() == b"DISK000"
    request = bridge.calls[-1]
    assert request["expectedIndex"] == 2
    assert request["expectedName"] == str(bridge.document)
    assert request["expectedSize"] == 7
    assert request["offset"] == 1
    assert result["display_start_address"] == 4096
    clean_requests(bridge)


def test_zero_length_read_does_not_require_an_export(bridge):
    bridge.emit_payload = False
    result, payload = bridge.transport.request("read", length=0)
    assert payload == b""
    assert result["size"] == 7


def test_unique_request_ids_do_not_reuse_files(bridge):
    bridge.transport.request("context")
    bridge.transport.request("context")
    assert bridge.calls[0]["requestId"] != bridge.calls[1]["requestId"]
    assert bridge.workdirs[0] != bridge.workdirs[1]
    clean_requests(bridge)


def test_renderer_does_not_reinterpret_placeholders_inside_file_names(bridge):
    name = str(bridge.document.parent / '@OFFSET@-@TEMPLATE@-quoted"-文件.bin')
    bridge.transport.request("read", index=2, name=name, size=7, offset=1, length=1)
    assert bridge.calls[-1]["expectedName"] == name


@pytest.mark.parametrize("version", ["12.0.0", "9.0.0"])
def test_unsupported_version_is_explicit(bridge, version):
    bridge.process = replace(bridge.process, version=version)
    status = bridge.transport.status()
    assert status["available"] is False
    assert status["error"]["code"] == "CAPABILITY_UNSUPPORTED"
    fails("CAPABILITY_UNSUPPORTED", bridge.transport.request, "context")
    assert bridge.calls == []


@pytest.mark.parametrize("code", ["BACKEND_UNAVAILABLE", "EDITOR_BUSY"])
def test_discovery_failure_is_actionable_and_does_not_dispatch(bridge, code):
    bridge.detect_error = BridgeError(code, "Resolve local editor activity.")
    assert bridge.transport.status()["error"]["code"] == code
    fails(code, bridge.transport.request, "context")
    assert bridge.calls == []


def test_unknown_operation_is_never_dispatched(bridge):
    fails("CAPABILITY_UNSUPPORTED", bridge.transport.request, "execute_arbitrary_script")
    assert bridge.calls == []


@pytest.mark.parametrize(
    "mutator",
    [
        lambda reply: reply.update(protocol=2),
        lambda reply: reply.update(protocol=True),
        lambda reply: reply.update(request_id="another-request"),
        lambda reply: reply.pop("result"),
        lambda reply: reply.update(result=[]),
        lambda reply: reply["result"].update(index=True),
        lambda reply: reply["result"].update(size=-1),
        lambda reply: reply["result"].update(dirty=2),
        lambda reply: reply["result"].update(cursor_offset=8),
        lambda reply: reply["result"].update(path=42),
        lambda reply: reply["result"].update(selection={"offset": 0}),
        lambda reply: reply["result"].update(selection={"offset": "0", "length": 0}),
        lambda reply: reply["result"].update(selection={"offset": -1, "length": 0}),
        lambda reply: reply["result"].update(selection={"offset": 6, "length": 2}),
        lambda reply: reply.update(error="invalid-error"),
        lambda reply: reply.update(error={"code": 2, "message": "bad"}),
    ],
)
def test_invalid_reply_schema_invalidates_session(bridge, mutator):
    bridge.mutate_reply = mutator
    fails("PROTOCOL_ERROR", bridge.transport.request, "context")
    assert bridge.transport.status()["available"] is False
    clean_requests(bridge)


@pytest.mark.parametrize("raw", [b"not-json", b"{", b"\xff", b"[]"])
def test_malformed_json_and_nonobject_responses(bridge, raw):
    bridge.raw_reply = raw
    fails("PROTOCOL_ERROR", bridge.transport.request, "context")
    assert bridge.transport.status()["available"] is False
    clean_requests(bridge)


def test_oversized_reply_is_rejected_before_json_parse(bridge):
    bridge.raw_reply = b" " * 16_385
    fails("PROTOCOL_ERROR", bridge.transport.request, "context")
    clean_requests(bridge)


def test_typed_editor_error_is_not_a_disk_fallback(bridge):
    def reply_error(reply):
        reply.pop("result")
        reply["error"] = {"code": "STALE_CONTEXT", "message": "The active document changed."}

    context = bridge.backend.context()
    bridge.mutate_reply = reply_error
    fails("STALE_CONTEXT", bridge.backend.read, context["buffer_id"], 0, 4)
    assert bridge.document.read_bytes() == b"DISK000"
    assert bridge.transport.status()["available"] is True
    clean_requests(bridge)


def test_missing_payload_never_reads_source_disk(bridge):
    context = bridge.backend.context()
    bridge.emit_payload = False
    fails("PROTOCOL_ERROR", bridge.backend.read, context["buffer_id"], 0, 4)
    assert bridge.document.read_bytes() == b"DISK000"
    clean_requests(bridge)


@pytest.mark.parametrize("payload,code", [(b"x", "FILE_CHANGED"), (b"12345", "LIMIT_EXCEEDED")])
def test_live_payload_length_must_match_exact_request(bridge, payload, code):
    context = bridge.backend.context()
    bridge.payload_override = payload
    fails(code, bridge.backend.read, context["buffer_id"], 0, 4)
    clean_requests(bridge)


def test_timeout_removes_cancellation_marker_and_invalidates_transport(bridge):
    bridge.emit_reply = False
    fails("EDITOR_TIMEOUT", bridge.transport.request, "context")
    assert bridge.transport.status()["available"] is False
    count = len(bridge.calls)
    fails("BACKEND_UNAVAILABLE", bridge.transport.request, "context")
    assert len(bridge.calls) == count
    clean_requests(bridge)


def test_dispatch_timeout_also_invalidates_session(bridge):
    bridge.failure = BridgeError("EDITOR_TIMEOUT", "Forwarding timeout")
    fails("EDITOR_TIMEOUT", bridge.transport.request, "select", offset=1, length=2)
    assert bridge.transport.status()["available"] is False
    clean_requests(bridge)


def test_editor_restart_after_verified_request_requires_reconnect(bridge):
    bridge.transport.request("context")
    bridge.process = replace(bridge.process, started=999)
    fails("EDITOR_DISCONNECTED", bridge.transport.request, "context")
    assert len(bridge.calls) == 1
    assert bridge.transport.status()["available"] is False


def test_editor_restart_during_first_request_is_detected(bridge):
    def restart(_request):
        bridge.process = replace(bridge.process, started=999)

    bridge.on_dispatch = restart
    fails("EDITOR_DISCONNECTED", bridge.transport.request, "context")
    assert bridge.transport.status()["available"] is False
    clean_requests(bridge)


def test_transport_mutex_reports_busy_without_dispatch(bridge):
    assert bridge.transport._mutex.acquire(blocking=False)
    try:
        fails("EDITOR_BUSY", bridge.transport.request, "context")
        assert bridge.calls == []
    finally:
        bridge.transport._mutex.release()
    bridge.transport.request("context")


def test_cross_transport_file_lock_serializes_one_editor(bridge):
    second = BridgeTransport(bridge.settings, detector=bridge.detect, dispatcher=bridge.dispatch)
    entered, release = threading.Event(), threading.Event()

    def blocked(_request):
        entered.set()
        assert release.wait(2), "Test dispatcher was not released"

    bridge.on_dispatch = blocked
    bridge.settings.editor_timeout = 2
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.transport.request, "context")
        assert entered.wait(1)
        try:
            fails("EDITOR_BUSY", second.request, "context")
        finally:
            release.set()
        future.result(timeout=3)
    clean_requests(bridge)


def test_backend_status_disables_atomic_buffer_apply(bridge):
    status = bridge.backend.status()
    assert status["capabilities"]["context"] is True
    assert status["capabilities"]["read"] is True
    assert status["capabilities"]["patch_apply"] is False
    assert "atomic" in status["patch_apply_reason"]
    fails("CAPABILITY_UNSUPPORTED", bridge.backend.patch_apply, "any", 0, "00", "ff", "any")
    assert bridge.calls == []


def test_backend_context_tokens_are_opaque_and_explicitly_live(bridge):
    first = bridge.backend.context()
    second = bridge.backend.context()
    assert first["buffer_id"] != second["buffer_id"]
    assert first["source"] == "editor"
    assert first["dirty"] is True and first["read_only"] is False
    assert "same-path reopen" in first["identity_scope"]
    assert "concurrent" in first["consistency"]


def test_unknown_context_never_dispatches(bridge):
    fails("STALE_CONTEXT", bridge.backend.read, "unknown", 0, 1)
    assert bridge.calls == []


def test_context_cannot_address_untitled_or_outside_root_buffers(bridge):
    bridge.context["path"] = "Untitled"
    context = bridge.backend.context()
    fails("CAPABILITY_UNSUPPORTED", bridge.backend.read, context["buffer_id"], 0, 1)
    bridge.context["path"] = str(bridge.document.parent.parent / "outside.bin")
    context = bridge.backend.context()
    fails("PATH_NOT_ALLOWED", bridge.backend.read, context["buffer_id"], 0, 1)
    assert len(bridge.calls) == 2


def test_context_expiry_and_bounded_storage(bridge, monkeypatch):
    bridge.settings.max_results = 1
    first = bridge.backend.context()
    second = bridge.backend.context()
    fails("STALE_CONTEXT", bridge.backend.read, first["buffer_id"], 0, 1)
    expires = bridge.backend._contexts[second["buffer_id"]][0]
    monkeypatch.setattr("editor010_mcp.editor_backend.time.monotonic", lambda: expires + 1)
    fails("STALE_CONTEXT", bridge.backend.read, second["buffer_id"], 0, 1)
    assert len(bridge.calls) == 2


@pytest.mark.parametrize("field,value", [("index", 3), ("path", "/outside.bin"), ("size", 8)])
def test_returned_target_must_still_match_requested_context(bridge, field, value):
    context = bridge.backend.context()
    bridge.mutate_reply = lambda reply: reply["result"].update({field: value})
    fails("STALE_CONTEXT", bridge.backend.read, context["buffer_id"], 0, 1)
    clean_requests(bridge)


@pytest.mark.parametrize(
    "offset,length,code",
    [
        (-1, 1, "INVALID_ARGUMENT"),
        (0, -1, "INVALID_ARGUMENT"),
        (True, 1, "INVALID_ARGUMENT"),
        (0, False, "INVALID_ARGUMENT"),
        (7, 1, "OUT_OF_BOUNDS"),
        (8, 0, "OUT_OF_BOUNDS"),
    ],
)
def test_backend_read_validates_ranges_before_dispatch(bridge, offset, length, code):
    context = bridge.backend.context()
    fails(code, bridge.backend.read, context["buffer_id"], offset, length)
    assert len(bridge.calls) == 1


def test_backend_read_limit_applies_before_export(bridge):
    context = bridge.backend.context()
    bridge.settings.max_read_bytes = 2
    fails("LIMIT_EXCEEDED", bridge.backend.read, context["buffer_id"], 0, 3)
    assert len(bridge.calls) == 1


def test_select_passes_explicit_target_and_zero_based_range(bridge):
    context = bridge.backend.context()
    result = bridge.backend.select(context["buffer_id"], 2, 3)
    assert result["selection"] == {"offset": 2, "length": 3}
    assert bridge.calls[-1]["expectedIndex"] == context["index"]
    assert bridge.calls[-1]["operation"] == "select"
    fails("OUT_OF_BOUNDS", bridge.backend.select, context["buffer_id"], 6, 2)


def test_template_requires_a_configured_trusted_location(bridge, tmp_path):
    context = bridge.backend.context()
    outside = tmp_path / "untrusted.bt"
    outside.write_text("uchar x;", encoding="utf-8")
    fails("PATH_NOT_ALLOWED", bridge.backend.run_template, context["buffer_id"], str(outside))
    wrong = bridge.templates / "script.1sc"
    wrong.write_text("", encoding="utf-8")
    fails("INVALID_ARGUMENT", bridge.backend.run_template, context["buffer_id"], str(wrong))
    bridge.settings.trusted_template_roots = ()
    fails(
        "CAPABILITY_UNSUPPORTED",
        bridge.backend.run_template,
        context["buffer_id"],
        str(bridge.template),
    )
    assert len(bridge.calls) == 1


def test_trusted_template_xml_is_structured_bounded_and_hashed(bridge):
    bridge.xml = (
        b"<results><variable><name>header</name><type>uchar</type><value>7</value>"
        b"<start>1001h</start><size>1</size></variable><variable><name>tail</name>"
        b"<start>1002h</start><size>1</size></variable></results>"
    )
    bridge.settings.max_results = 1
    context = bridge.backend.context()
    result = bridge.backend.run_template(context["buffer_id"], str(bridge.template))
    assert result["xml_root"] == "results"
    assert result["xml"] == bridge.xml.decode()
    assert result["xml_bytes"] == len(bridge.xml)
    assert result["template_sha256"] == hashlib.sha256(bridge.template.read_bytes()).hexdigest()
    assert result["variables"][0]["offset"] == 1
    assert result["variables"][0]["length"] == 1
    assert result["variables"][0]["name"] == "header"
    assert result["variables_truncated"] is True
    assert bridge.calls[-1]["templatePath"] == bridge.template.as_posix()
    assert result["source"] == "editor"
    clean_requests(bridge)


@pytest.mark.parametrize(
    "payload",
    [
        b"not XML",
        b"<results>",
        b'<!DOCTYPE x [<!ENTITY x "unsafe">]><x>&x;</x>',
        b"<!doctype x><x/>",
    ],
)
def test_invalid_or_dtd_xml_is_rejected(bridge, payload):
    context = bridge.backend.context()
    bridge.payload_override = payload
    fails("PROTOCOL_ERROR", bridge.backend.run_template, context["buffer_id"], str(bridge.template))


def test_template_export_size_limit(bridge):
    context = bridge.backend.context()
    bridge.settings.max_xml_bytes = 8
    fails("LIMIT_EXCEEDED", bridge.backend.run_template, context["buffer_id"], str(bridge.template))
    clean_requests(bridge)


def test_template_rejects_utf16_before_entity_expansion(bridge):
    context = bridge.backend.context()
    bridge.payload_override = '<!DOCTYPE x [<!ENTITY x "unsafe">]><x>&x;</x>'.encode("utf-16")
    fails("PROTOCOL_ERROR", bridge.backend.run_template, context["buffer_id"], str(bridge.template))


def test_template_changed_during_execution_is_a_conflict(bridge):
    context = bridge.backend.context()
    bridge.on_dispatch = lambda _request: bridge.template.write_text(
        "uchar other;", encoding="utf-8"
    )
    fails("FILE_CHANGED", bridge.backend.run_template, context["buffer_id"], str(bridge.template))


def test_patch_preview_reads_unsaved_bytes_and_never_mutates(bridge):
    context = bridge.backend.context()
    before = bridge.live
    preview = bridge.backend.patch_preview(context["buffer_id"], 1, "4c495645", "61626364")
    assert preview["before_hex"] == "4c495645"
    assert preview["after_hex"] == "61626364"
    assert preview["can_apply"] is False
    assert preview["apply_error"] == "CAPABILITY_UNSUPPORTED"
    assert bridge.calls[-1]["operation"] == "read"
    assert bridge.live == before
    assert bridge.document.read_bytes() == b"DISK000"
    fails("PRECONDITION_FAILED", bridge.backend.patch_preview, context["buffer_id"], 0, "44", "ff")


@pytest.mark.parametrize(
    "expected,replacement", [("", ""), ("0", "00"), ("zz", "00"), ("00", "gg"), ("00", "0011")]
)
def test_patch_preview_hex_and_length_validation(bridge, expected, replacement):
    context = bridge.backend.context()
    fails(
        "INVALID_ARGUMENT",
        bridge.backend.patch_preview,
        context["buffer_id"],
        0,
        expected,
        replacement,
    )
    assert len(bridge.calls) == 1
