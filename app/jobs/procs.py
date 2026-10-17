"""Owned child processes: Windows Job Object (kill-on-close) or POSIX process group.

Only processes this supervisor spawned are ever signalled; nothing by name or port.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0100, 0x0001

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]


class ProcessJob:
    """One kill-on-close container per owned process tree.

    Closing the handle (explicitly, or by the OS when the supervisor dies) kills
    the process and every descendant that did not break away.
    """

    def __init__(self) -> None:
        self._handle = None
        if sys.platform == "win32":
            handle = _k32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation,
                                                ctypes.byref(info), ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                _k32.CloseHandle(handle)
                raise OSError(err, "SetInformationJobObject failed")
            self._handle = handle

    def assign(self, pid: int) -> None:
        if sys.platform != "win32":
            return  # POSIX: children are started in their own session (see popen_kwargs).
        proc = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not _k32.AssignProcessToJobObject(self._handle, proc):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            _k32.CloseHandle(proc)

    def kill_all(self) -> None:
        if sys.platform == "win32" and self._handle:
            _k32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if sys.platform == "win32" and self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


def popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            _k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_tree(pid: int, job: ProcessJob, terminate, wait_exit, grace_sec: float) -> bool:
    """Terminate the owned root, then kill the whole owned tree; True once the root exited.

    ``terminate()`` and ``wait_exit(timeout) -> bool`` act on the handle we hold.
    """
    if not wait_exit(0):
        try:
            terminate()
            if sys.platform != "win32":
                os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
    exited = wait_exit(grace_sec)
    job.kill_all()  # Descendants (decoders) may outlive the root.
    if sys.platform != "win32":
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + max(grace_sec, 1.0)
    while not exited and time.monotonic() < deadline:
        exited = wait_exit(0.1)
    return exited
