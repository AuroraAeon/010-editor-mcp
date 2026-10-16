"""Deterministic binary contracts using disposable files, never user data."""

from __future__ import annotations

import hashlib
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from editor010_mcp.models import BridgeError, Settings
from editor010_mcp.standalone import BinaryEngine


@pytest.fixture
def engine(tmp_path: Path) -> BinaryEngine:
    return BinaryEngine(Settings(allowed_roots=(tmp_path,), chunk_size=3))


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "sample.bin"
    path.write_bytes(bytes.fromhex("00112233445566778899aabbccddeeff"))
    return path


def assert_error(code: str, operation, *args, **kwargs) -> BridgeError:
    with pytest.raises(BridgeError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def test_inspect_metadata_and_streamed_hashes(engine, sample):
    data = sample.read_bytes()
    result = engine.inspect(sample, ["sha256", "md5", "sha256"])
    assert result["source"] == "disk"
    assert result["path"] == str(sample.resolve())
    assert result["size"] == len(data)
    assert len(result["file_id"]) == 64
    assert result["modified_time_ns"] == sample.stat().st_mtime_ns
    assert result["hashes"] == {
        "sha256": hashlib.sha256(data).hexdigest(),
        "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
    }


@pytest.mark.parametrize("hashes", [["shake_128"], ["SHA256"], [1], "sha256"])
def test_invalid_hashes(engine, sample, hashes):
    assert_error("INVALID_ARGUMENT", engine.inspect, sample, hashes)


def test_metadata_does_not_scan_a_large_file(engine, tmp_path, monkeypatch):
    path = tmp_path / "large.bin"
    with path.open("wb") as handle:
        handle.truncate(1_000_000)
    engine.settings.max_scan_bytes = 10

    def no_open(*args, **kwargs):
        pytest.fail("Metadata-only inspection must not read the file")

    monkeypatch.setattr(Path, "open", no_open)
    assert engine.inspect(path)["size"] == 1_000_000
    assert_error("LIMIT_EXCEEDED", engine.inspect, path, ["sha256"])


def test_read_exact_zero_based_bytes_and_safe_preview(engine, tmp_path):
    path = tmp_path / "text.bin"
    path.write_bytes(b"\x00\x01\x02\x03A\n\x1b\xff~\x7f")
    result = engine.read(path, 3, 7)
    assert result["offset"] == 3
    assert result["length"] == 7
    assert result["hex"] == "03410a1bff7e7f"
    assert result["text_preview"] == ".A...~."
    assert "text_preview" not in engine.read(path, 0, 4, False)
    # Raw bytes retain their on-disk order; no host-endian conversion occurs.
    assert engine.read(path, 0, 4)["hex"] == "00010203"


@pytest.mark.parametrize(
    "offset,length,code",
    [
        (-1, 1, "INVALID_ARGUMENT"),
        (0, -1, "INVALID_ARGUMENT"),
        (True, 1, "INVALID_ARGUMENT"),
        (0, False, "INVALID_ARGUMENT"),
        (0.5, 1, "INVALID_ARGUMENT"),
        (0, "1", "INVALID_ARGUMENT"),
        (16, 1, "OUT_OF_BOUNDS"),
        (17, 0, "OUT_OF_BOUNDS"),
        (15, 2, "OUT_OF_BOUNDS"),
    ],
)
def test_read_invalid_ranges(engine, sample, offset, length, code):
    assert_error(code, engine.read, sample, offset, length)


def test_read_limit_and_preview_type(engine, sample):
    engine.settings.max_read_bytes = 4
    assert_error("LIMIT_EXCEEDED", engine.read, sample, 0, 5)
    assert_error("INVALID_ARGUMENT", engine.read, sample, 0, 1, "yes")


def test_empty_file_and_eof_operations(engine, tmp_path, sample):
    empty = tmp_path / "empty.bin"
    empty.touch()
    assert engine.inspect(empty, ["sha256"])["hashes"]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert engine.read(empty, 0, 0)["hex"] == ""
    assert engine.read(sample, 16, 0)["hex"] == ""
    assert engine.search(empty, "00")["matches"] == []
    assert engine.diff(empty, empty)["ranges"] == []
    assert_error("OUT_OF_BOUNDS", engine.read, empty, 0, 1)


def test_search_overlaps_crosses_chunks_and_respects_range(engine, tmp_path):
    path = tmp_path / "repeated.bin"
    path.write_bytes(b"xAAAAAxyAAAAAy")
    result = engine.search(path, "41 41 41 41")
    assert result["matches"] == [
        {"offset": 1, "length": 4},
        {"offset": 2, "length": 4},
        {"offset": 8, "length": 4},
        {"offset": 9, "length": 4},
    ]
    assert result["bytes_scanned"] == 14
    assert result["truncated"] is False
    assert engine.search(path, "41414141", offset=2, length=8)["matches"] == [
        {"offset": 2, "length": 4}
    ]
    assert engine.search(path, "41414141", offset=3, length=2)["matches"] == []


def test_search_reports_actual_result_truncation(engine, tmp_path):
    path = tmp_path / "repeat.bin"
    path.write_bytes(b"AAAA")
    result = engine.search(path, "4141", max_results=2)
    assert result["matches"] == [{"offset": 0, "length": 2}, {"offset": 1, "length": 2}]
    assert result["truncated"] is True
    exact = engine.search(path, "4141", max_results=3)
    assert len(exact["matches"]) == 3
    assert exact["truncated"] is False


@pytest.mark.parametrize("pattern", ["", "  ", "0", "gg", "0x12", "12-34", "12;34", None])
def test_search_malformed_or_empty_hex(engine, sample, pattern):
    assert_error("INVALID_ARGUMENT", engine.search, sample, pattern)


@pytest.mark.parametrize(
    "limit,code",
    [
        (0, "INVALID_ARGUMENT"),
        (-1, "INVALID_ARGUMENT"),
        (True, "INVALID_ARGUMENT"),
        (1.5, "INVALID_ARGUMENT"),
        (1001, "LIMIT_EXCEEDED"),
    ],
)
def test_search_result_limit(engine, sample, limit, code):
    assert_error(code, engine.search, sample, "00", max_results=limit)


def test_scan_limits_allow_explicit_bounded_work(engine, sample):
    engine.settings.max_scan_bytes = 4
    assert_error("LIMIT_EXCEEDED", engine.search, sample, "00")
    assert engine.search(sample, "00", length=4)["matches"] == [{"offset": 0, "length": 1}]
    assert_error("LIMIT_EXCEEDED", engine.diff, sample, sample)
    assert engine.diff(sample, sample, offset=12, length=4)["ranges"] == []
    assert_error("OUT_OF_BOUNDS", engine.search, sample, "00", offset=17)
    assert_error("OUT_OF_BOUNDS", engine.diff, sample, sample, offset=17)


def test_diff_precise_ranges_merge_across_chunk_boundaries(engine, tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"0123456789")
    b.write_bytes(b"0ab34cde89")
    result = engine.diff(a, b)
    assert result["ranges"] == [
        {
            "offset": 1,
            "length": 2,
            "a_length": 2,
            "b_length": 2,
            "a_hex": b"12".hex(),
            "b_hex": b"ab".hex(),
            "preview_truncated": False,
        },
        {
            "offset": 5,
            "length": 3,
            "a_length": 3,
            "b_length": 3,
            "a_hex": b"567".hex(),
            "b_hex": b"cde".hex(),
            "preview_truncated": False,
        },
    ]
    assert result["bytes_scanned"] == 10
    assert result["truncated"] is False
    assert engine.diff(a, b, offset=6, length=2)["ranges"][0]["offset"] == 6


@pytest.mark.parametrize("reverse", [False, True])
def test_diff_unequal_lengths_and_empty_side(engine, tmp_path, reverse):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"AB")
    b.write_bytes(b"ABXYZ")
    if reverse:
        a, b = b, a
    result = engine.diff(a, b)["ranges"][0]
    assert result["offset"] == 2 and result["length"] == 3
    assert result["a_length"] == (3 if reverse else 0)
    assert result["b_length"] == (0 if reverse else 3)
    a.write_bytes(b"")
    assert engine.diff(a, b)["ranges"][0]["offset"] == 0


def test_diff_result_limit_retains_exact_last_range(engine, tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"abcdefg")
    b.write_bytes(b"aXXdeYg")
    result = engine.diff(a, b, max_ranges=1)
    assert [(r["offset"], r["length"]) for r in result["ranges"]] == [(1, 2)]
    assert result["truncated"] is True
    b.write_bytes(b"aXXdefg")
    assert engine.diff(a, b, max_ranges=1)["truncated"] is False


def test_diff_large_range_keeps_bounded_previews(engine, tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"a" * 10000)
    b.write_bytes(b"b" * 10000)
    engine.settings.chunk_size = 128
    result = engine.diff(a, b, max_ranges=1)["ranges"][0]
    assert result["length"] == 10000
    assert len(result["a_hex"]) == len(result["b_hex"]) == 64
    assert result["preview_truncated"] is True


def test_search_and_diff_against_small_deterministic_oracles(engine, tmp_path):
    rng = random.Random(1010)
    a_path, b_path = tmp_path / "oracle-a.bin", tmp_path / "oracle-b.bin"
    for _ in range(40):
        a = bytes(rng.randrange(4) for _ in range(rng.randrange(40)))
        b = bytes(rng.randrange(4) for _ in range(rng.randrange(40)))
        pattern = bytes(rng.randrange(4) for _ in range(rng.randrange(1, 8)))
        a_path.write_bytes(a)
        b_path.write_bytes(b)
        expected_matches = [i for i in range(len(a)) if a[i : i + len(pattern)] == pattern]
        assert [
            m["offset"] for m in engine.search(a_path, pattern.hex())["matches"]
        ] == expected_matches

        changed = [i for i in range(max(len(a), len(b))) if a[i : i + 1] != b[i : i + 1]]
        expected_ranges = []
        for i in changed:
            if expected_ranges and expected_ranges[-1][0] + expected_ranges[-1][1] == i:
                expected_ranges[-1][1] += 1
            else:
                expected_ranges.append([i, 1])
        actual = engine.diff(a_path, b_path)
        assert [[r["offset"], r["length"]] for r in actual["ranges"]] == expected_ranges


def test_large_sparse_file_seeks_and_streams(engine, tmp_path):
    path = tmp_path / "sparse.bin"
    with path.open("wb") as handle:
        handle.seek(2_000_000)
        handle.write(b"END")
    engine.settings.chunk_size = 4096
    assert engine.read(path, 2_000_000, 3)["hex"] == b"END".hex()
    found = engine.search(path, b"END".hex(), offset=1_999_998, length=5)
    assert found["matches"] == [{"offset": 2_000_000, "length": 3}]
    assert (
        engine.inspect(path, ["sha256"])["hashes"]["sha256"]
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "expected,replacement",
    [("", ""), ("00", ""), ("00", "1122"), ("0", "00"), ("xx", "00"), ("00", "zz")],
)
def test_patch_invalid_hex_or_size(engine, sample, expected, replacement):
    assert_error("INVALID_ARGUMENT", engine.patch_preview, sample, 0, expected, replacement)


def test_patch_preview_precondition_and_bounds(engine, sample):
    original = sample.read_bytes()
    assert_error("PRECONDITION_FAILED", engine.patch_preview, sample, 0, "ff", "ee")
    assert_error("OUT_OF_BOUNDS", engine.patch_preview, sample, 16, "00", "ee")
    assert_error("INVALID_ARGUMENT", engine.patch_preview, sample, -1, "00", "ee")
    engine.settings.max_patch_bytes = 1
    assert_error("LIMIT_EXCEEDED", engine.patch_preview, sample, 0, "0011", "1122")
    assert sample.read_bytes() == original


def test_patch_copy_across_chunks_preserves_source(engine, sample, tmp_path):
    original = sample.read_bytes()
    preview = engine.patch_preview(sample, 2, "22334455", "aabbccdd")
    assert preview["before_hex"] == "22334455"
    assert preview["after_hex"] == "aabbccdd"
    assert sample.read_bytes() == original
    output = tmp_path / "patched.bin"
    result = engine.patch_apply(
        sample, 2, "22 33 44 55", "AA BB CC DD", output, preview["preview_token"]
    )
    assert result["original_unchanged"] is True
    assert result["source_file_id"] == preview["file_id"]
    assert result["output_path"] == str(output.resolve())
    assert output.read_bytes() == original[:2] + bytes.fromhex("aabbccdd") + original[6:]
    assert sample.read_bytes() == original
    assert_error(
        "PREVIEW_REQUIRED",
        engine.patch_apply,
        sample,
        2,
        "22334455",
        "aabbccdd",
        tmp_path / "replay.bin",
        preview["preview_token"],
    )


def test_patch_requires_preview_bound_to_all_arguments(engine, sample, tmp_path):
    output = tmp_path / "out.bin"
    assert_error("PREVIEW_REQUIRED", engine.patch_apply, sample, 0, "00", "ee", output, "guessed")
    preview = engine.patch_preview(sample, 0, "00", "ee")
    assert_error(
        "PRECONDITION_FAILED",
        engine.patch_apply,
        sample,
        0,
        "00",
        "ff",
        output,
        preview["preview_token"],
    )
    assert_error(
        "PRECONDITION_FAILED",
        engine.patch_apply,
        sample,
        1,
        "11",
        "ee",
        output,
        preview["preview_token"],
    )
    other = tmp_path / "other.bin"
    other.write_bytes(sample.read_bytes())
    assert_error(
        "PRECONDITION_FAILED",
        engine.patch_apply,
        other,
        0,
        "00",
        "ee",
        output,
        preview["preview_token"],
    )
    assert not output.exists()


def test_patch_refuses_existing_output_and_in_place(engine, sample, tmp_path):
    preview = engine.patch_preview(sample, 0, "00", "ee")
    output = tmp_path / "existing.bin"
    output.write_bytes(b"keep")
    assert_error(
        "OUTPUT_EXISTS", engine.patch_apply, sample, 0, "00", "ee", output, preview["preview_token"]
    )
    assert output.read_bytes() == b"keep"
    assert_error(
        "CAPABILITY_UNSUPPORTED",
        engine.patch_apply,
        sample,
        0,
        "00",
        "ee",
        sample,
        preview["preview_token"],
    )
    assert sample.read_bytes().startswith(b"\x00")


def test_patch_preview_expires_without_sleep(engine, sample, tmp_path, monkeypatch):
    preview = engine.patch_preview(sample, 0, "00", "ee")
    expires = engine._previews[preview["preview_token"]].expires
    monkeypatch.setattr("editor010_mcp.standalone.time.monotonic", lambda: expires + 1)
    assert_error(
        "STALE_PREVIEW",
        engine.patch_apply,
        sample,
        0,
        "00",
        "ee",
        tmp_path / "out.bin",
        preview["preview_token"],
    )
    assert not (tmp_path / "out.bin").exists()


def test_patch_rejects_change_outside_patched_range(engine, sample, tmp_path):
    preview = engine.patch_preview(sample, 0, "00", "ee")
    data = sample.read_bytes()
    sample.write_bytes(data[:-1] + b"Z")
    output = tmp_path / "out.bin"
    assert_error(
        "STALE_PREVIEW", engine.patch_apply, sample, 0, "00", "ee", output, preview["preview_token"]
    )
    assert not output.exists()


def test_patch_copy_limit(engine, sample, tmp_path):
    engine.settings.max_copy_bytes = 8
    preview = engine.patch_preview(sample, 0, "00", "ee")
    assert_error(
        "LIMIT_EXCEEDED",
        engine.patch_apply,
        sample,
        0,
        "00",
        "ee",
        tmp_path / "out.bin",
        preview["preview_token"],
    )
    assert not (tmp_path / "out.bin").exists()


def test_patch_failure_removes_partial_output(engine, sample, tmp_path, monkeypatch):
    original = sample.read_bytes()
    preview = engine.patch_preview(sample, 0, "00", "ee")
    validate = engine._unchanged
    calls = 0

    def changed_after_copy(path, handle, file_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BridgeError("FILE_CHANGED", "Simulated concurrent source mutation")
        validate(path, handle, file_id)

    monkeypatch.setattr(engine, "_unchanged", changed_after_copy)
    output = tmp_path / "partial.bin"
    assert_error(
        "FILE_CHANGED", engine.patch_apply, sample, 0, "00", "ee", output, preview["preview_token"]
    )
    assert not output.exists()
    assert sample.read_bytes() == original


def test_preview_storage_bounded_and_expiry_reclaims_it(engine, sample, monkeypatch):
    engine.settings.max_results = 1
    preview = engine.patch_preview(sample, 0, "00", "ee")
    assert_error("LIMIT_EXCEEDED", engine.patch_preview, sample, 0, "00", "ff")
    expires = engine._previews[preview["preview_token"]].expires
    monkeypatch.setattr("editor010_mcp.standalone.time.monotonic", lambda: expires + 1)
    assert engine.patch_preview(sample, 0, "00", "ff")["preview_token"] != preview["preview_token"]


def test_concurrent_apply_can_consume_a_preview_only_once(engine, sample, tmp_path):
    preview = engine.patch_preview(sample, 0, "00", "ee")

    def apply(index):
        try:
            return engine.patch_apply(
                sample, 0, "00", "ee", tmp_path / f"out{index}.bin", preview["preview_token"]
            )
        except BridgeError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("PREVIEW_REQUIRED") == 1
    assert len(list(tmp_path.glob("out*.bin"))) == 1


def test_path_policy_missing_outside_directory_and_device(engine, sample, tmp_path):
    assert_error("PATH_NOT_FOUND", engine.read, tmp_path / "missing.bin", 0, 0)
    assert_error("PATH_NOT_ALLOWED", engine.read, tmp_path, 0, 0)
    assert_error("PATH_NOT_ALLOWED", engine.read, tmp_path.parent / "outside.bin", 0, 0)
    assert_error("PATH_NOT_ALLOWED", engine.read, r"\\.\PhysicalDrive0", 0, 1)
    preview = engine.patch_preview(sample, 0, "00", "ee")
    assert_error(
        "PATH_NOT_ALLOWED",
        engine.patch_apply,
        sample,
        0,
        "00",
        "ee",
        tmp_path.parent / "out.bin",
        preview["preview_token"],
    )


def test_engine_accepts_its_own_independent_settings(tmp_path):
    settings = Settings(allowed_roots=(tmp_path,))
    small = BinaryEngine(replace(settings, max_read_bytes=1))
    path = tmp_path / "two.bin"
    path.write_bytes(b"AB")
    assert_error("LIMIT_EXCEEDED", small.read, path, 0, 2)
