"""Read-only Windows process discovery. Importable on other platforms."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .models import BridgeError, Settings


@dataclass(frozen=True)
class EditorProcess:
    pid: int
    started: int
    exe: Path
    window: int
    version: str

    @property
    def identity(self) -> str:
        return f"{self.pid}:{self.started}"


def discover(settings: Settings) -> EditorProcess:
    if os.name != "nt":
        raise BridgeError("BACKEND_UNAVAILABLE", "The live bridge currently requires Windows.")
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    user = c.WinDLL("user32", use_last_error=True)

    class Entry(c.Structure):
        _fields_ = [
            ("dwSize", w.DWORD),
            ("cntUsage", w.DWORD),
            ("th32ProcessID", w.DWORD),
            ("th32DefaultHeapID", c.c_size_t),
            ("th32ModuleID", w.DWORD),
            ("cntThreads", w.DWORD),
            ("th32ParentProcessID", w.DWORD),
            ("pcPriClassBase", w.LONG),
            ("dwFlags", w.DWORD),
            ("szExeFile", w.WCHAR * 260),
        ]

    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.GetProcessTimes.argtypes = [
        w.HANDLE,
        c.POINTER(w.FILETIME),
        c.POINTER(w.FILETIME),
        c.POINTER(w.FILETIME),
        c.POINTER(w.FILETIME),
    ]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    entry = Entry()
    entry.dwSize = c.sizeof(entry)
    candidates: list[tuple[int, int, Path]] = []
    try:
        has = kernel.Process32FirstW(snapshot, c.byref(entry))
        while has:
            if entry.szExeFile.lower() == "010editor.exe":
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        text = c.create_unicode_buffer(32768)
                        length = w.DWORD(len(text))
                        created, exited, kt, ut = (
                            w.FILETIME(),
                            w.FILETIME(),
                            w.FILETIME(),
                            w.FILETIME(),
                        )
                        if kernel.QueryFullProcessImageNameW(
                            handle, 0, text, c.byref(length)
                        ) and kernel.GetProcessTimes(
                            handle, c.byref(created), c.byref(exited), c.byref(kt), c.byref(ut)
                        ):
                            exe = Path(text.value).resolve()
                            candidates.append(
                                (
                                    entry.th32ProcessID,
                                    (created.dwHighDateTime << 32) | created.dwLowDateTime,
                                    exe,
                                )
                            )
                    finally:
                        kernel.CloseHandle(handle)
            has = kernel.Process32NextW(snapshot, c.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if not candidates:
        raise BridgeError(
            "BACKEND_UNAVAILABLE",
            "Start 010 Editor manually, enable single-instance mode, then retry. "
            "The bridge never starts an editor session.",
        )
    # Forwarding cannot choose among multiple sessions, even with an explicit PID.
    if len(candidates) != 1:
        raise BridgeError(
            "BACKEND_UNAVAILABLE",
            "Multiple 010 Editor processes found. Keep one intended GUI session open.",
        )
    pid, started, exe = candidates[0]
    if (settings.editor_pid is not None and pid != settings.editor_pid) or (
        settings.editor_exe is not None and exe != settings.editor_exe
    ):
        raise BridgeError(
            "BACKEND_UNAVAILABLE",
            "Configured editor PID/executable does not match the running session.",
        )
    windows: list[int] = []
    callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    for name in ("IsWindowVisible", "IsWindowEnabled", "IsHungAppWindow"):
        getattr(user, name).argtypes = [w.HWND]

    @callback
    def visit(hwnd: int, _: int) -> bool:
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, c.byref(owner))
        cls = c.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, cls, len(cls))
        if owner.value == pid and user.IsWindowVisible(hwnd) and cls.value == "Q010GoodWindowClass":
            windows.append(int(hwnd))
        return True

    user.EnumWindows(visit, 0)
    if not windows:
        raise BridgeError(
            "BACKEND_UNAVAILABLE",
            "The intended editor has no visible GUI window; dismiss startup dialogs manually.",
        )
    window = windows[0]
    if not user.IsWindowEnabled(window) or user.IsHungAppWindow(window):
        raise BridgeError(
            "EDITOR_BUSY",
            "The editor is blocked by a dialog or is not responding. Resolve it manually.",
        )
    return EditorProcess(pid, started, exe, window, _version(exe))


def _version(exe: Path) -> str:
    import ctypes as c
    from ctypes import wintypes as w

    version = c.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [w.LPCWSTR, c.POINTER(w.DWORD)]
    version.GetFileVersionInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p]
    version.VerQueryValueW.argtypes = [
        c.c_void_p,
        w.LPCWSTR,
        c.POINTER(c.c_void_p),
        c.POINTER(w.UINT),
    ]
    unused = w.DWORD()
    size = version.GetFileVersionInfoSizeW(str(exe), c.byref(unused))
    if not size:
        return "unknown"
    data = c.create_string_buffer(size)
    pointer, length = c.c_void_p(), w.UINT()
    if not version.GetFileVersionInfoW(str(exe), 0, size, data) or not version.VerQueryValueW(
        data, "\\", c.byref(pointer), c.byref(length)
    ):
        return "unknown"
    fixed = c.cast(pointer, c.POINTER(w.DWORD))
    return f"{fixed[2] >> 16}.{fixed[2] & 65535}.{fixed[3] >> 16}.{fixed[3] & 65535}"
