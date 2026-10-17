"""Owned child processes: Windows Job Object (kill-on-close) or POSIX process group.

Only processes spawned here are ever signalled; nothing by name or port.
On Windows a process is created suspended and resumed only after it is inside its
Job Object, so it cannot start descendants outside the job.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _JobObjectBasicAccountingInformation = 1
    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0100, 0x0001
    _CREATE_SUSPENDED = 0x00000004

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

    class _Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.c_void_p]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


class ProcessJob:
    """One kill-on-close container per owned process tree.

    Closing the handle (explicitly, or by the OS when the owner dies) kills every
    member. On POSIX the recorded process groups play this role (no orphan guard).
    """

    def __init__(self) -> None:
        self._handle = None
        self._groups: set[int] = set()
        self._lock = threading.Lock()  # A closed handle value may be reused by the OS.
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

    @property
    def closed(self) -> bool:
        return sys.platform == "win32" and self._handle is None

    def assign(self, pid: int, handle: int | None = None) -> None:
        if sys.platform != "win32":
            self._groups.add(pid)  # Started with its own session/group.
            return
        with self._lock:
            self._assign_locked(pid, handle)

    def _assign_locked(self, pid: int, handle: int | None) -> None:
        if self._handle is None:
            raise OSError("job is closed")
        own = handle is None
        if own:
            handle = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not handle:
                raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not _k32.AssignProcessToJobObject(self._handle, handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            if own:
                _k32.CloseHandle(handle)

    def active(self) -> int:
        """Number of live members (Windows: whole tree; POSIX: live groups)."""
        if sys.platform == "win32":
            with self._lock:
                if self._handle is None:
                    return 0
                info = _Accounting()
                if not _k32.QueryInformationJobObject(self._handle, _JobObjectBasicAccountingInformation,
                                                      ctypes.byref(info), ctypes.sizeof(info), None):
                    raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
                return int(info.ActiveProcesses)
        alive = 0
        for group in self._groups:
            try:
                os.killpg(group, 0)
                alive += 1
            except OSError:
                pass
        return alive

    def kill_all(self) -> None:
        if sys.platform == "win32":
            with self._lock:
                if self._handle:
                    _k32.TerminateJobObject(self._handle, 1)
            return
        for group in self._groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass

    def close(self) -> None:
        if sys.platform == "win32":
            with self._lock:
                if self._handle:
                    _k32.CloseHandle(self._handle)
                    self._handle = None


def popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_in_job(argv: list[str], job: ProcessJob, **kwargs) -> subprocess.Popen:
    """Popen whose first instruction runs only after the process is inside ``job``."""
    options = popen_kwargs()
    options.update(kwargs)
    if sys.platform == "win32":
        options["creationflags"] = options.get("creationflags", 0) | _CREATE_SUSPENDED
    proc = subprocess.Popen(argv, **options)
    try:
        if sys.platform == "win32":
            handle = int(proc._handle)  # noqa: SLF001 - full-access handle owned by Popen.
            job.assign(proc.pid, handle)
            if _ntdll.NtResumeProcess(handle) != 0:
                raise OSError("NtResumeProcess failed")
        else:
            job.assign(proc.pid)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return proc


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


def stop_tree(job: ProcessJob, terminate, wait_exit, grace_sec: float) -> bool:
    """Terminate the owned root, kill the whole owned tree, confirm it is gone.

    ``terminate()`` and ``wait_exit(timeout) -> bool`` act on the root handle we
    hold. True only when the root exited AND the job has no live members.
    """
    if not wait_exit(0):
        try:
            terminate()
        except OSError:
            pass
    exited = wait_exit(grace_sec)
    job.kill_all()  # Descendants (decoders, venv launcher children) may outlive the root.
    deadline = time.monotonic() + max(grace_sec, 1.0)
    while True:
        exited = exited or wait_exit(0)
        try:
            empty = job.active() == 0
        except OSError:
            empty = False
        if exited and empty:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class ProcessOwner:
    """Registry of short-lived jobs (pre-admission decoders) for owner shutdown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: set[ProcessJob] = set()
        self._closed = False

    def new_job(self) -> ProcessJob:
        with self._lock:
            if self._closed:
                raise RuntimeError("process owner is shut down")
            job = ProcessJob()
            self._jobs.add(job)
            return job

    def release(self, job: ProcessJob) -> None:
        job.kill_all()
        job.close()
        with self._lock:
            self._jobs.discard(job)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            jobs, self._jobs = list(self._jobs), set()
        for job in jobs:
            job.kill_all()
            job.close()
