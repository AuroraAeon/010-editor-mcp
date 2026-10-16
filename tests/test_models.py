"""Configuration, path policy, identities and validation primitives."""

from __future__ import annotations

import math
import os

import pytest

from editor010_mcp.models import (
    BridgeError,
    Settings,
    file_identity,
    parse_hex,
    resolve_allowed_path,
    validate_range,
)


def error_code(code, operation, *args, **kwargs):
    with pytest.raises(BridgeError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def test_machine_readable_error():
    error = BridgeError("EXAMPLE", "Review this.", {"offset": 3})
    assert str(error) == "Review this."
    assert error.as_dict() == {
        "code": "EXAMPLE",
        "message": "Review this.",
        "details": {"offset": 3},
    }
    assert BridgeError("EXAMPLE", "Review this.").as_dict()["details"] == {}


@pytest.mark.parametrize(
    "value,expected", [("", b""), ("00 ff 7F", b"\x00\xff\x7f"), ("AB\nCD\tEF", b"\xab\xcd\xef")]
)
def test_parse_hex(value, expected):
    assert parse_hex(value) == expected


@pytest.mark.parametrize("value", [None, b"00", 12, "f", "0x00", "00:ff", "00-ff", "zz", "12\x00"])
def test_parse_hex_invalid(value):
    error_code("INVALID_ARGUMENT", parse_hex, value)


def test_parse_hex_limit():
    assert parse_hex("00ff", max_bytes=2) == b"\x00\xff"
    error_code("LIMIT_EXCEEDED", parse_hex, "00ff", max_bytes=1)


@pytest.mark.parametrize("offset,length,size", [(0, 0, 0), (0, 3, 3), (3, 0, 3), (2, 1, 3)])
def test_validate_range_valid(offset, length, size):
    validate_range(offset, length, size)


@pytest.mark.parametrize(
    "offset,length,size",
    [
        (-1, 0, 1),
        (0, -1, 1),
        (0, 0, -1),
        (True, 0, 1),
        (0, True, 1),
        (0, 0, True),
        (0.1, 0, 1),
        (0, "1", 1),
        (0, 1, 1.0),
    ],
)
def test_validate_range_rejects_invalid_types(offset, length, size):
    error_code("INVALID_ARGUMENT", validate_range, offset, length, size)


def test_validate_range_bounds_and_work_limit():
    error_code("OUT_OF_BOUNDS", validate_range, 2, 0, 1)
    error_code("OUT_OF_BOUNDS", validate_range, 1, 2, 2)
    error_code("LIMIT_EXCEEDED", validate_range, 0, 3, 3, 2)
    validate_range(0, 2, 3, 2)


@pytest.mark.parametrize(
    "name",
    [
        "max_read_bytes",
        "max_scan_bytes",
        "max_results",
        "chunk_size",
        "max_patch_bytes",
        "max_copy_bytes",
        "max_xml_bytes",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, math.nan, math.inf, "1", None])
def test_settings_integer_fields(name, value, tmp_path):
    error_code("INVALID_ARGUMENT", Settings, allowed_roots=(tmp_path,), **{name: value})


@pytest.mark.parametrize("name", ["preview_ttl_seconds", "editor_timeout"])
@pytest.mark.parametrize("value", [0, -1, True, math.nan, math.inf, -math.inf, "1", None])
def test_settings_finite_timeouts(name, value, tmp_path):
    error_code("INVALID_ARGUMENT", Settings, allowed_roots=(tmp_path,), **{name: value})


@pytest.mark.parametrize("value", [0, -1, True, 1.2, "1"])
def test_settings_editor_pid(value, tmp_path):
    error_code("INVALID_ARGUMENT", Settings, allowed_roots=(tmp_path,), editor_pid=value)


def test_settings_paths_and_empty_roots(tmp_path):
    settings = Settings(
        allowed_roots=(tmp_path / "sub" / "..",),
        trusted_template_roots=(tmp_path,),
        editor_exe=tmp_path / "010editor.exe",
        bridge_dir=tmp_path / "bridge",
        editor_pid=123,
        editor_timeout=0.5,
    )
    assert settings.allowed_roots == (tmp_path.resolve(),)
    assert settings.trusted_template_roots == (tmp_path.resolve(),)
    assert settings.editor_exe == (tmp_path / "010editor.exe").resolve()
    assert settings.bridge_dir == (tmp_path / "bridge").resolve()
    error_code("INVALID_ARGUMENT", Settings, allowed_roots=())
    error_code("INVALID_ARGUMENT", Settings, allowed_roots=(None,))


def test_settings_environment_parsing(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("EDITOR010_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("EDITOR010_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("EDITOR010_TRUSTED_TEMPLATE_ROOTS", str(tmp_path))
    monkeypatch.setenv("EDITOR010_EDITOR_EXE", str(tmp_path / "010editor.exe"))
    monkeypatch.setenv("EDITOR010_BRIDGE_DIR", str(tmp_path / "bridge"))
    monkeypatch.setenv("EDITOR010_MAX_READ_BYTES", "123")
    monkeypatch.setenv("EDITOR010_EDITOR_PID", "456")
    monkeypatch.setenv("EDITOR010_EDITOR_TIMEOUT", "1.25")
    result = Settings.from_env()
    assert result.allowed_roots == (tmp_path.resolve(),)
    assert result.max_read_bytes == 123
    assert result.editor_pid == 456
    assert result.editor_timeout == 1.25
    monkeypatch.setenv("EDITOR010_MAX_READ_BYTES", "oops")
    error_code("INVALID_ARGUMENT", Settings.from_env)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0"])
def test_invalid_numeric_environment(tmp_path, monkeypatch, value):
    monkeypatch.setenv("EDITOR010_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("EDITOR010_EDITOR_TIMEOUT", value)
    error_code("INVALID_ARGUMENT", Settings.from_env)


def test_path_resolution_and_new_output(tmp_path, monkeypatch):
    path = tmp_path / "a.bin"
    path.write_bytes(b"A")
    roots = (tmp_path,)
    monkeypatch.chdir(tmp_path)
    assert resolve_allowed_path("a.bin", roots) == path.resolve()
    assert (
        resolve_allowed_path(tmp_path / "new.bin", roots, must_exist=False) == tmp_path / "new.bin"
    )
    error_code("PATH_NOT_FOUND", resolve_allowed_path, tmp_path / "absent.bin", roots)
    error_code(
        "PATH_NOT_FOUND",
        resolve_allowed_path,
        tmp_path / "absent" / "output.bin",
        roots,
        must_exist=False,
    )
    error_code("PATH_NOT_ALLOWED", resolve_allowed_path, tmp_path, roots)
    error_code("PATH_NOT_ALLOWED", resolve_allowed_path, tmp_path.parent / "outside.bin", roots)
    error_code("INVALID_ARGUMENT", resolve_allowed_path, None, roots)


@pytest.mark.parametrize(
    "path",
    [
        "",
        " ",
        "NUL",
        "con.txt",
        "COM1",
        "LPT2.bin",
        "CON .txt",
        "dir/NUL.bin",
        r"dir\AUX",
        "COM¹",
        "a:b",
        "C:relative.bin",
        "file.bin:stream",
        r"\\server\share\file.bin",
        "//server/share/file.bin",
        r"/\server\share\file.bin",
        r"\\.\PhysicalDrive0",
        r"\\?\C:\file.bin",
        "bad\x00.bin",
    ],
)
def test_forbidden_paths_even_on_portable_backend(path, tmp_path):
    error_code("PATH_NOT_ALLOWED", resolve_allowed_path, path, (tmp_path,), must_exist=False)


def test_symlink_escape_is_rejected_when_supported(tmp_path):
    outside = tmp_path / "outside"
    inside = tmp_path / "inside"
    outside.mkdir()
    inside.mkdir()
    target = outside / "private.bin"
    target.write_bytes(b"private")
    link = inside / "link.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links.")
    error_code("PATH_NOT_ALLOWED", resolve_allowed_path, link, (inside,))


def test_file_identity_is_consistent_between_fd_and_path(tmp_path):
    path = tmp_path / "identity.bin"
    path.write_bytes(b"first")
    path.write_bytes(b"second")
    with path.open("rb") as handle:
        assert file_identity(path, os.fstat(handle.fileno())) == file_identity(path)
    previous = file_identity(path)
    path.write_bytes(b"longer-content")
    assert file_identity(path) != previous
