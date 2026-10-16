"""Opt-in real Windows GUI acceptance; never starts an editor process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.live_editor
@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("EDITOR010_RUN_LIVE") != "1",
    reason="Requires Windows, an existing GUI, and explicit EDITOR010_RUN_LIVE=1.",
)
def test_real_editor_through_mcp_stdio(tmp_path: Path) -> None:
    exe = os.environ.get("EDITOR010_EDITOR_EXE")
    if not exe:
        pytest.skip("Set EDITOR010_EDITOR_EXE to the installed executable.")
    args = [
        sys.executable,
        "-m",
        "editor010_mcp.verify_live",
        "--create-fixture",
        "--editor-exe",
        exe,
        "--work-dir",
        str(tmp_path / "live"),
    ]
    if pid := os.environ.get("EDITOR010_EDITOR_PID"):
        args.extend(["--editor-pid", pid])
    completed = subprocess.run(args, capture_output=True, text=True, timeout=180)
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, report
    assert report["status"] == "passed"
    assert report["cleanup"] == "passed"
    assert report["process_preserved"] is True
    assert "unsaved_live_read" in report["checks"]
    assert "trusted_template_xml_from_unsaved_buffer" in report["checks"]
