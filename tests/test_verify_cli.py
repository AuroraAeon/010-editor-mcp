"""Verifier lifecycle tests never contact a GUI or launch an editor."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from editor010_mcp import verify_live
from editor010_mcp.models import BridgeError
from editor010_mcp.windows import EditorProcess


@pytest.fixture
def environment(tmp_path, monkeypatch):
    process = EditorProcess(101, 202, tmp_path / "010Editor.exe", 303, "16.0.4")
    prior = tmp_path / "prior.bin"
    prior.write_bytes(b"prior")
    calls = {"probe": [], "launch": []}
    monkeypatch.setattr(verify_live, "discover", lambda _: process)

    class Backend:
        def __init__(self, _):
            pass

        def context(self):
            return {"index": 0, "path": str(prior)}

    monkeypatch.setattr(verify_live, "EditorBackend", Backend)
    monkeypatch.setattr(
        verify_live.subprocess, "run", lambda args, **kw: calls["launch"].append(args)
    )

    def probe(settings, selected, work, body):
        assert selected == process
        assert (work / "probe.bin").read_bytes() == verify_live.DISK
        assert (work / "probe.bt").is_file()
        calls["probe"].append((work, body))

    monkeypatch.setattr(verify_live, "_probe", probe)

    async def exercise(*args):
        return ["mcp_stdio_discovery", "unsaved_live_read"]

    monkeypatch.setattr(verify_live, "_exercise", exercise)
    return SimpleNamespace(
        process=process,
        prior=prior,
        calls=calls,
        args=[
            "--create-fixture",
            "--editor-exe",
            str(process.exe),
            "--work-dir",
            str(tmp_path / "work"),
        ],
    )


def invoke(args, capsys, expected=0):
    with pytest.raises(SystemExit) as caught:
        verify_live.main(args)
    assert caught.value.code == expected
    return json.loads(capsys.readouterr().out)


def test_fixture_authorization_is_required(environment):
    with pytest.raises(SystemExit) as caught:
        verify_live.main(environment.args[1:])
    assert caught.value.code == 2
    assert environment.calls == {"probe": [], "launch": []}


def test_missing_editor_never_dispatches(environment, monkeypatch, capsys):
    def absent(_):
        raise BridgeError("BACKEND_UNAVAILABLE", "No editor is running.")

    monkeypatch.setattr(verify_live, "discover", absent)
    report = invoke(environment.args, capsys, 1)
    assert report["error"]["code"] == "BACKEND_UNAVAILABLE"
    assert environment.calls == {"probe": [], "launch": []}


def test_success_cleans_owned_files_and_restores_prior(environment, capsys):
    report = invoke(environment.args, capsys)
    assert report["cleanup"] == "passed"
    assert report["process_preserved"] is True
    setup, cleanup = environment.calls["probe"]
    assert setup[0] == cleanup[0] and not setup[0].exists()
    assert environment.calls["launch"] == [
        [str(environment.process.exe), str(setup[0] / "probe.bin")],
        [str(environment.process.exe), str(environment.prior)],
    ]
    assert json.dumps(str(environment.prior)) in cleanup[1]
    assert "<0" in cleanup[1]
    assert "FileClose()" in cleanup[1]
    assert environment.prior.read_bytes() == b"prior"


def test_cleanup_failure_retains_only_owned_artifacts(environment, monkeypatch, capsys):
    original = verify_live._probe

    def fail_cleanup(*args):
        original(*args)
        if len(environment.calls["probe"]) == 2:
            raise BridgeError("VERIFICATION_FAILED", "fixture_cleanup_failed")

    monkeypatch.setattr(verify_live, "_probe", fail_cleanup)
    report = invoke(environment.args, capsys, 1)
    assert report["cleanup"] == "failed"
    assert report["artifacts_retained"] is True
    owned = environment.calls["probe"][0][0]
    assert (owned / "probe.bin").is_file()
    assert str(owned) not in json.dumps(report)
    assert environment.prior.read_bytes() == b"prior"


def test_nested_client_failure_reports_check_and_still_cleans(environment, monkeypatch, capsys):
    async def fail(*args):
        raise ExceptionGroup("client", [BridgeError("VERIFICATION_FAILED", "known_check")])

    monkeypatch.setattr(verify_live, "_exercise", fail)
    report = invoke(environment.args, capsys, 1)
    assert report["error"] == {"code": "VERIFICATION_FAILED", "check": "known_check"}
    assert report["cleanup"] == "passed"
    assert not environment.calls["probe"][0][0].exists()


def test_partial_setup_failure_attempts_owned_cleanup(environment, monkeypatch, capsys):
    original = verify_live._probe

    def fail_setup(*args):
        original(*args)
        if len(environment.calls["probe"]) == 1:
            raise BridgeError("VERIFICATION_FAILED", "setup_failed")

    monkeypatch.setattr(verify_live, "_probe", fail_setup)
    report = invoke(environment.args, capsys, 1)
    assert report["cleanup"] == "passed"
    assert len(environment.calls["probe"]) == 2


def test_owned_file_cleanup_waits_for_editor_handle(tmp_path, monkeypatch):
    owned = tmp_path / "probe.bin"
    owned.write_bytes(b"fixture")
    original = Path.unlink
    attempts = []

    def unlink(path, **kwargs):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("Editor still holds the closed fixture.")
        original(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(verify_live.time, "sleep", lambda _: None)
    verify_live._remove_owned_file(owned, 1)
    assert attempts == [owned, owned, owned]
    assert not owned.exists()


def test_owned_file_cleanup_timeout_retains_file(tmp_path, monkeypatch):
    owned = tmp_path / "probe.bin"
    owned.write_bytes(b"fixture")

    def locked(path, **kwargs):
        raise PermissionError("Editor handle remains locked.")

    ticks = iter((0, 2))
    monkeypatch.setattr(Path, "unlink", locked)
    monkeypatch.setattr(verify_live.time, "monotonic", lambda: next(ticks))
    with pytest.raises(PermissionError):
        verify_live._remove_owned_file(owned, 1)
    assert owned.read_bytes() == b"fixture"


def test_empty_editor_cleanup_uses_only_owned_anchor(environment, monkeypatch, capsys):
    class EmptyBackend:
        def __init__(self, _):
            pass

        def context(self):
            if not environment.calls["probe"]:
                raise BridgeError("NO_ACTIVE_BUFFER", "No document is active.")
            owned = environment.calls["probe"][0][0]
            return {"index": 0, "path": str(owned / "cleanup.bin")}

    monkeypatch.setattr(verify_live, "EditorBackend", EmptyBackend)
    report = invoke(environment.args, capsys)
    assert report["cleanup"] == "passed"
    owned = environment.calls["probe"][0][0]
    assert environment.calls["launch"] == [
        [str(environment.process.exe), str(owned / "probe.bin")],
        [str(environment.process.exe), str(owned / "cleanup.bin")],
        [str(environment.process.exe), "-close"],
    ]
    assert len(environment.calls["probe"]) == 3
    assert not owned.exists()
    assert environment.prior.read_bytes() == b"prior"
