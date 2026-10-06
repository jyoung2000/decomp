"""Win32 implementation of rebuild_controller.sandbox (ctypes only; imported lazily on Windows)."""
from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import time
from ctypes import wintypes as wt
from pathlib import Path
from typing import Any, Callable

from .sandbox import (NETWORK_BLOCKED_APPCONTAINER, NETWORK_OPEN, IsolationPolicy, RunResult, SandboxedProcess, SandboxError,
                      _feed, _Pump)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
uenv = ctypes.WinDLL("userenv", use_last_error=True)

HANDLE = wt.HANDLE
LPVOID = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

CREATE_SUSPENDED = 0x4
CREATE_NEW_CONSOLE = 0x10
CREATE_NEW_PROCESS_GROUP = 0x200
CREATE_UNICODE_ENVIRONMENT = 0x400
EXTENDED_STARTUPINFO_PRESENT = 0x80000
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x100
HANDLE_FLAG_INHERIT = 0x1
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x20009

TOKEN_ASSIGN_PRIMARY, TOKEN_DUPLICATE, TOKEN_QUERY, TOKEN_ADJUST_DEFAULT = 0x1, 0x2, 0x8, 0x80
SecurityImpersonation, TokenPrimary, TokenIntegrityLevel = 2, 1, 25
SE_GROUP_INTEGRITY = 0x20

JobObjectBasicAccountingInformation = 1
JobObjectBasicUIRestrictions = 4
JobObjectAssociateCompletionPortInformation = 7
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
UI_HANDLES, UI_READCLIPBOARD, UI_WRITECLIPBOARD, UI_SYSTEMPARAMETERS = 0x1, 0x2, 0x4, 0x8
UI_DISPLAYSETTINGS, UI_GLOBALATOMS, UI_DESKTOP, UI_EXITWINDOWS = 0x10, 0x20, 0x40, 0x80
UI_SETS = {
    "strict": UI_HANDLES | UI_READCLIPBOARD | UI_WRITECLIPBOARD | UI_SYSTEMPARAMETERS | UI_DISPLAYSETTINGS | UI_GLOBALATOMS | UI_DESKTOP | UI_EXITWINDOWS,
    "interactive": UI_SYSTEMPARAMETERS | UI_DISPLAYSETTINGS | UI_DESKTOP | UI_EXITWINDOWS,
    "none": 0,
}
UI_NAMES = {UI_HANDLES: "handles", UI_READCLIPBOARD: "read_clipboard", UI_WRITECLIPBOARD: "write_clipboard", UI_SYSTEMPARAMETERS: "system_parameters",
            UI_DISPLAYSETTINGS: "display_settings", UI_GLOBALATOMS: "global_atoms", UI_DESKTOP: "desktop", UI_EXITWINDOWS: "exit_windows"}
MSG_ACTIVE_PROCESS_LIMIT, MSG_ACTIVE_PROCESS_ZERO, MSG_NEW_PROCESS, MSG_ABNORMAL_EXIT = 3, 4, 6, 8
MSG_PROCESS_MEMORY_LIMIT, MSG_JOB_MEMORY_LIMIT = 9, 10

WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x10
SDDL_REVISION_1 = 1
APPCONTAINER_NAME = "RebuildStudio.Isolation"


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", LPVOID), ("bInheritHandle", wt.BOOL)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", LPVOID),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                               "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T), ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wt.DWORD)]


class JOBOBJECT_ASSOCIATE_COMPLETION_PORT(ctypes.Structure):
    _fields_ = [("CompletionKey", LPVOID), ("CompletionPort", HANDLE)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64), ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64), ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", LPVOID), ("Attributes", wt.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", LPVOID), ("Capabilities", LPVOID), ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]


def _proto(dll, name, res, *args):
    f = getattr(dll, name)
    f.restype, f.argtypes = res, list(args)
    return f


CloseHandle = _proto(k32, "CloseHandle", wt.BOOL, HANDLE)
CreatePipe = _proto(k32, "CreatePipe", wt.BOOL, ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), wt.DWORD)
SetHandleInformation = _proto(k32, "SetHandleInformation", wt.BOOL, HANDLE, wt.DWORD, wt.DWORD)
CreateJobObjectW = _proto(k32, "CreateJobObjectW", HANDLE, LPVOID, wt.LPCWSTR)
SetInformationJobObject = _proto(k32, "SetInformationJobObject", wt.BOOL, HANDLE, ctypes.c_int, LPVOID, wt.DWORD)
QueryInformationJobObject = _proto(k32, "QueryInformationJobObject", wt.BOOL, HANDLE, ctypes.c_int, LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD))
AssignProcessToJobObject = _proto(k32, "AssignProcessToJobObject", wt.BOOL, HANDLE, HANDLE)
TerminateJobObject = _proto(k32, "TerminateJobObject", wt.BOOL, HANDLE, wt.UINT)
CreateIoCompletionPort = _proto(k32, "CreateIoCompletionPort", HANDLE, HANDLE, HANDLE, ctypes.c_size_t, wt.DWORD)
GetQueuedCompletionStatus = _proto(k32, "GetQueuedCompletionStatus", wt.BOOL, HANDLE, ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.c_size_t),
                                   ctypes.POINTER(LPVOID), wt.DWORD)
ResumeThread = _proto(k32, "ResumeThread", wt.DWORD, HANDLE)
TerminateProcess = _proto(k32, "TerminateProcess", wt.BOOL, HANDLE, wt.UINT)
WaitForSingleObject = _proto(k32, "WaitForSingleObject", wt.DWORD, HANDLE, wt.DWORD)
GetExitCodeProcess = _proto(k32, "GetExitCodeProcess", wt.BOOL, HANDLE, ctypes.POINTER(wt.DWORD))
GetCurrentProcess = _proto(k32, "GetCurrentProcess", HANDLE)
InitializeProcThreadAttributeList = _proto(k32, "InitializeProcThreadAttributeList", wt.BOOL, LPVOID, wt.DWORD, wt.DWORD, ctypes.POINTER(SIZE_T))
UpdateProcThreadAttribute = _proto(k32, "UpdateProcThreadAttribute", wt.BOOL, LPVOID, wt.DWORD, ctypes.c_size_t, LPVOID, SIZE_T, LPVOID, LPVOID)
DeleteProcThreadAttributeList = _proto(k32, "DeleteProcThreadAttributeList", None, LPVOID)
CreateProcessW = _proto(k32, "CreateProcessW", wt.BOOL, wt.LPCWSTR, wt.LPWSTR, LPVOID, LPVOID, wt.BOOL, wt.DWORD, LPVOID, wt.LPCWSTR,
                        LPVOID, ctypes.POINTER(PROCESS_INFORMATION))
LocalFree = _proto(k32, "LocalFree", LPVOID, LPVOID)

OpenProcessToken = _proto(adv, "OpenProcessToken", wt.BOOL, HANDLE, wt.DWORD, ctypes.POINTER(HANDLE))
DuplicateTokenEx = _proto(adv, "DuplicateTokenEx", wt.BOOL, HANDLE, wt.DWORD, LPVOID, ctypes.c_int, ctypes.c_int, ctypes.POINTER(HANDLE))
SetTokenInformation = _proto(adv, "SetTokenInformation", wt.BOOL, HANDLE, ctypes.c_int, LPVOID, wt.DWORD)
ConvertStringSidToSidW = _proto(adv, "ConvertStringSidToSidW", wt.BOOL, wt.LPCWSTR, ctypes.POINTER(LPVOID))
ConvertSidToStringSidW = _proto(adv, "ConvertSidToStringSidW", wt.BOOL, LPVOID, ctypes.POINTER(wt.LPWSTR))
GetLengthSid = _proto(adv, "GetLengthSid", wt.DWORD, LPVOID)
FreeSid = _proto(adv, "FreeSid", LPVOID, LPVOID)
CreateProcessAsUserW = _proto(adv, "CreateProcessAsUserW", wt.BOOL, HANDLE, wt.LPCWSTR, wt.LPWSTR, LPVOID, LPVOID, wt.BOOL, wt.DWORD, LPVOID,
                              wt.LPCWSTR, LPVOID, ctypes.POINTER(PROCESS_INFORMATION))
ConvertStringSecurityDescriptorToSecurityDescriptorW = _proto(adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wt.BOOL, wt.LPCWSTR,
                                                              wt.DWORD, ctypes.POINTER(LPVOID), ctypes.POINTER(wt.ULONG))
GetSecurityDescriptorSacl = _proto(adv, "GetSecurityDescriptorSacl", wt.BOOL, LPVOID, ctypes.POINTER(wt.BOOL), ctypes.POINTER(LPVOID), ctypes.POINTER(wt.BOOL))
SetNamedSecurityInfoW = _proto(adv, "SetNamedSecurityInfoW", wt.DWORD, wt.LPWSTR, ctypes.c_int, wt.DWORD, LPVOID, LPVOID, LPVOID, LPVOID)
DeriveAppContainerSidFromAppContainerName = _proto(uenv, "DeriveAppContainerSidFromAppContainerName", ctypes.c_long, wt.LPCWSTR, ctypes.POINTER(LPVOID))
CreateAppContainerProfile = _proto(uenv, "CreateAppContainerProfile", ctypes.c_long, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, LPVOID, wt.DWORD,
                                   ctypes.POINTER(LPVOID))
DeleteAppContainerProfile = _proto(uenv, "DeleteAppContainerProfile", ctypes.c_long, wt.LPCWSTR)
HRESULT_ALREADY_EXISTS = 0x800700B7


def _check(ok, what: str):
    if not ok:
        err = ctypes.get_last_error()
        raise SandboxError(f"{what} failed: [WinError {err}] {ctypes.FormatError(err).strip()}")
    return ok


def _close(h) -> None:
    if h:
        CloseHandle(h)


# ------------------------------------------------------------------------------------------------ labels / grants
def label_low(path: Path) -> None:
    """Give ``path`` (and, by inheritance, its contents) a Low mandatory label so a low-IL process can write there."""
    sd = LPVOID()
    _check(ConvertStringSecurityDescriptorToSecurityDescriptorW("S:(ML;OICI;NW;;;LW)", SDDL_REVISION_1, ctypes.byref(sd), None), "SDDL convert")
    try:
        present, defaulted = wt.BOOL(), wt.BOOL()
        sacl = LPVOID()
        _check(GetSecurityDescriptorSacl(sd, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)), "GetSecurityDescriptorSacl")
        rc = SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, sacl)
        if rc != 0:
            raise SandboxError(f"could not set Low integrity label on {path}: [WinError {rc}] {ctypes.FormatError(rc).strip()}")
    finally:
        LocalFree(sd)


def ensure_appcontainer_profile() -> bool:
    """Create the per-user AppContainer profile used by 'appcontainer' mode (no admin needed). Returns True if created now.

    This registers a per-user container named RebuildStudio.Isolation (an HKCU AppContainer mapping and a per-user Packages folder);
    :func:`remove_appcontainer_profile` deletes it again.
    """
    sid = LPVOID()
    hr = CreateAppContainerProfile(APPCONTAINER_NAME, "Rebuild Studio isolation", "Rebuild Studio: runs untrusted programs without network",
                                   None, 0, ctypes.byref(sid))
    if hr == 0:
        FreeSid(sid)
        return True
    if (hr & 0xffffffff) == HRESULT_ALREADY_EXISTS:
        return False
    raise SandboxError(f"CreateAppContainerProfile failed: HRESULT 0x{hr & 0xffffffff:08x}")


def remove_appcontainer_profile() -> None:
    DeleteAppContainerProfile(APPCONTAINER_NAME)


def _appcontainer_sid() -> tuple[LPVOID, str]:
    sid = LPVOID()
    hr = DeriveAppContainerSidFromAppContainerName(APPCONTAINER_NAME, ctypes.byref(sid))
    if hr != 0:
        raise SandboxError(f"DeriveAppContainerSidFromAppContainerName failed: HRESULT 0x{hr & 0xffffffff:08x}")
    s = wt.LPWSTR()
    _check(ConvertSidToStringSidW(sid, ctypes.byref(s)), "ConvertSidToStringSid")
    text = s.value
    LocalFree(s)
    return sid, text


def grant_appcontainer(path: Path, *, full: bool) -> None:
    _sid, text = _appcontainer_sid()
    FreeSid(_sid)
    icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
    perm = "(OI)(CI)F" if full else "(OI)(CI)RX"
    r = subprocess.run([icacls, str(path), "/grant", f"*{text}:{perm}", "/T", "/C", "/Q"], capture_output=True, timeout=120,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise SandboxError(f"icacls grant for AppContainer failed on {path}: {r.stdout.decode('mbcs', 'replace')}{r.stderr.decode('mbcs', 'replace')}")


# ------------------------------------------------------------------------------------------------ tokens
def _low_token() -> HANDLE:
    tok, dup = HANDLE(), HANDLE()
    _check(OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT, ctypes.byref(tok)),
           "OpenProcessToken")
    try:
        _check(DuplicateTokenEx(tok, TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT, None, SecurityImpersonation,
                                TokenPrimary, ctypes.byref(dup)), "DuplicateTokenEx")
    finally:
        _close(tok)
    sid = LPVOID()
    try:
        _check(ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)), "ConvertStringSidToSid(Low)")
        tml = TOKEN_MANDATORY_LABEL(SID_AND_ATTRIBUTES(sid, SE_GROUP_INTEGRITY))
        _check(SetTokenInformation(dup, TokenIntegrityLevel, ctypes.byref(tml), ctypes.sizeof(tml) + GetLengthSid(sid)), "SetTokenInformation(Low IL)")
    except SandboxError:
        _close(dup)
        raise
    finally:
        if sid:
            LocalFree(sid)
    return dup


# ------------------------------------------------------------------------------------------------ job objects
def _make_job(policy: IsolationPolicy) -> tuple[HANDLE, HANDLE, list[str]]:
    job = CreateJobObjectW(None, None)
    _check(job, "CreateJobObject")
    applied = ["kill_on_job_close", "die_on_unhandled_exception", "no_breakaway"]
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if policy.process_memory_bytes:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY; info.ProcessMemoryLimit = policy.process_memory_bytes; applied.append("process_memory")
    if policy.job_memory_bytes:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY; info.JobMemoryLimit = policy.job_memory_bytes; applied.append("job_memory")
    if policy.max_processes:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS; info.BasicLimitInformation.ActiveProcessLimit = policy.max_processes; applied.append("active_processes")
    info.BasicLimitInformation.LimitFlags = flags
    try:
        _check(SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)), "SetInformationJobObject(limits)")
        ui = UI_SETS[policy.ui_restrictions]
        if ui:
            u = JOBOBJECT_BASIC_UI_RESTRICTIONS(ui)
            _check(SetInformationJobObject(job, JobObjectBasicUIRestrictions, ctypes.byref(u), ctypes.sizeof(u)), "SetInformationJobObject(ui)")
            applied.append("ui:" + ",".join(n for bit, n in UI_NAMES.items() if ui & bit))
        port = CreateIoCompletionPort(INVALID_HANDLE_VALUE, None, 0, 1)
        _check(port, "CreateIoCompletionPort")
        acp = JOBOBJECT_ASSOCIATE_COMPLETION_PORT(None, port)
        if not SetInformationJobObject(job, JobObjectAssociateCompletionPortInformation, ctypes.byref(acp), ctypes.sizeof(acp)):
            _close(port); port = None
    except SandboxError:
        _close(job)
        raise
    return job, port, applied


def _drain_port(port, triggered: set[str], timeout_ms: int = 0) -> None:
    if not port:
        return
    msg, key, ov = wt.DWORD(), ctypes.c_size_t(), LPVOID()
    while GetQueuedCompletionStatus(port, ctypes.byref(msg), ctypes.byref(key), ctypes.byref(ov), timeout_ms):
        m = msg.value
        if m == MSG_PROCESS_MEMORY_LIMIT:
            triggered.add("process_memory")
        elif m == MSG_JOB_MEMORY_LIMIT:
            triggered.add("job_memory")
        elif m == MSG_ACTIVE_PROCESS_LIMIT:
            triggered.add("active_processes")
        timeout_ms = 0


def _accounting(job) -> JOBOBJECT_BASIC_ACCOUNTING_INFORMATION:
    acc = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    QueryInformationJobObject(job, JobObjectBasicAccountingInformation, ctypes.byref(acc), ctypes.sizeof(acc), None)
    return acc


# ------------------------------------------------------------------------------------------------ process creation
def _resolve(argv0: str, env: dict[str, str], cwd: Path) -> str:
    p = Path(argv0)
    if p.is_absolute():
        return str(p)
    if any(sep in argv0 for sep in ("\\", "/")):
        return str((cwd / p).resolve())
    exts = [""] + [e.lower() for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    for d in env.get("PATH", "").split(os.pathsep):
        for e in exts:
            cand = Path(d) / (argv0 + e)
            if d and cand.is_file():
                return str(cand)
    raise SandboxError(f"program {argv0!r} not found on the isolated PATH")


def _command_line(argv: list[str], env: dict[str, str]) -> tuple[str, str]:
    exe = argv[0]
    if exe.lower().endswith((".cmd", ".bat")):
        comspec = env.get("ComSpec") or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
        return comspec, f'"{comspec}" /d /s /c "{subprocess.list2cmdline(argv)}"'
    return exe, subprocess.list2cmdline(argv)


def _env_block(env: dict[str, str]):
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    s = "".join(f"{k}={v}\0" for k, v in items if "=" not in k[1:] and "\0" not in k + v) + "\0"
    return ctypes.create_unicode_buffer(s, len(s))


def _isolation_info(policy: IsolationPolicy, mode: str, applied: list[str], downgrade: str | None) -> dict[str, Any]:
    return {"platform": "win32", "mode": mode, "integrity": {"low": "Low (S-1-16-4096)", "medium": "Medium (unchanged user token)",
                                                             "appcontainer": "AppContainer (Low, no capabilities)"}[mode],
            "job_object": True, "job_limits": applied, "suspended_start": True, "inherited_handles": "std handles only",
            "env": "allowlist", "network": NETWORK_BLOCKED_APPCONTAINER if mode == "appcontainer" else NETWORK_OPEN,
            "filesystem": {"low": "writes denied to medium-integrity objects (user profile, documents, install folders); reads NOT restricted",
                           "medium": "not confined: can read and write everything the user can",
                           "appcontainer": "only objects granted to ALL APPLICATION PACKAGES or the container SID (system dirs read-only, work dir)"}[mode],
            "downgrade": downgrade}


class _Started:
    def __init__(self, pi: PROCESS_INFORMATION, job, port, applied, mode, downgrade):
        self.pi, self.job, self.port, self.applied, self.mode, self.downgrade = pi, job, port, applied, mode, downgrade


def _create(argv: list[str], *, cwd: Path, env: dict[str, str], policy: IsolationPolicy, std: tuple | None, console: str) -> _Started:
    argv = [_resolve(argv[0], env, cwd)] + list(argv[1:])
    app, cmdline = _command_line(argv, env)
    mode = policy.integrity
    downgrade = f"configured: {policy.downgrade_reason}" if mode == "medium" else None
    token = None
    if mode == "low":
        try:
            token = _low_token()
        except SandboxError as e:
            if not policy.allow_downgrade:
                raise SandboxError(f"low integrity isolation unavailable on this host ({e}); set isolation.allow_downgrade or choose "
                                   "integrity=medium with a reason") from e
            mode, downgrade = "medium", f"automatic: low integrity token unavailable ({e})"
    job, port, applied = _make_job(policy)
    attr_buf = None
    sid = None
    handles = None
    try:
        n_attrs = (1 if std else 0) + (1 if mode == "appcontainer" else 0)
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
        flags |= CREATE_NO_WINDOW if console == "none" else (CREATE_NEW_CONSOLE if console == "new" else 0)
        if std:
            si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = std
        if n_attrs:
            size = SIZE_T(0)
            InitializeProcThreadAttributeList(None, n_attrs, 0, ctypes.byref(size))
            attr_buf = ctypes.create_string_buffer(size.value)
            _check(InitializeProcThreadAttributeList(attr_buf, n_attrs, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
            si.lpAttributeList = ctypes.cast(attr_buf, LPVOID)
            if std:
                uniq = []
                for h in std:
                    if h and h not in uniq:
                        uniq.append(h)
                handles = (HANDLE * len(uniq))(*uniq)
                _check(UpdateProcThreadAttribute(attr_buf, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, ctypes.sizeof(handles), None, None),
                       "UpdateProcThreadAttribute(handle list)")
            if mode == "appcontainer":
                ensure_appcontainer_profile()
                sid, _ = _appcontainer_sid()
                caps = SECURITY_CAPABILITIES(sid, None, 0, 0)
                _check(UpdateProcThreadAttribute(attr_buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(caps), ctypes.sizeof(caps), None, None),
                       "UpdateProcThreadAttribute(security capabilities)")
        pi = PROCESS_INFORMATION()
        block = _env_block(env)
        cmd_buf = ctypes.create_unicode_buffer(cmdline)
        inherit = bool(std)
        if token is not None:
            ok = CreateProcessAsUserW(token, app, cmd_buf, None, None, inherit, flags, block, str(cwd), ctypes.byref(si), ctypes.byref(pi))
            what = "CreateProcessAsUser(low integrity)"
        else:
            ok = CreateProcessW(app, cmd_buf, None, None, inherit, flags, block, str(cwd), ctypes.byref(si), ctypes.byref(pi))
            what = "CreateProcess"
        if not ok:
            err = ctypes.get_last_error()
            raise SandboxError(f"{what} for {argv[0]} failed: [WinError {err}] {ctypes.FormatError(err).strip()}")
        if not AssignProcessToJobObject(job, pi.hProcess):
            err = ctypes.get_last_error()
            TerminateProcess(pi.hProcess, 1)
            _close(pi.hThread); _close(pi.hProcess)
            raise SandboxError(f"AssignProcessToJobObject failed: [WinError {err}] {ctypes.FormatError(err).strip()}")
        ResumeThread(pi.hThread)
        _close(pi.hThread); pi.hThread = None
        return _Started(pi, job, port, applied, mode, downgrade)
    except BaseException:
        _close(job); _close(port)
        raise
    finally:
        if attr_buf is not None:
            DeleteProcThreadAttributeList(attr_buf)
        if sid:
            FreeSid(sid)
        _close(token)


def _pipe(inherit_child_end_is_read: bool):
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    r, w = HANDLE(), HANDLE()
    _check(CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0), "CreatePipe")
    parent = w if inherit_child_end_is_read else r
    _check(SetHandleInformation(parent, HANDLE_FLAG_INHERIT, 0), "SetHandleInformation")
    return r, w


def _to_file(h, mode: str):
    fd = msvcrt.open_osfhandle(h.value if isinstance(h, HANDLE) else h, os.O_RDONLY if "r" in mode else os.O_WRONLY)
    return os.fdopen(fd, mode, buffering=0)


def _exit_code(hproc) -> int | None:
    code = wt.DWORD()
    if GetExitCodeProcess(hproc, ctypes.byref(code)):
        return None if code.value == 259 else code.value  # STILL_ACTIVE
    return None


def run(argv, *, work: Path, cwd: Path, policy: IsolationPolicy, env: dict[str, str], stdin: bytes | None,
        poll: Callable[[], None] | None, label: str = "") -> RunResult:
    in_r, in_w = _pipe(True)
    out_r, out_w = _pipe(False)
    err_r, err_w = _pipe(False)
    try:
        started = _create(argv, cwd=cwd, env=env, policy=policy, std=(in_r, out_w, err_w), console="none")
    except BaseException:
        for h in (in_r, in_w, out_r, out_w, err_r, err_w):
            _close(h)
        raise
    for h in (in_r, out_w, err_w):   # child owns these now
        _close(h)
    fin, fout, ferr = _to_file(in_w, "wb"), _to_file(out_r, "rb"), _to_file(err_r, "rb")
    po, pe = _Pump(fout.read, policy.stdout_cap_bytes), _Pump(ferr.read, policy.stderr_cap_bytes)
    po.start(); pe.start()
    _feed(fin.write, fin.close, stdin)
    start = time.monotonic()
    triggered: set[str] = set()
    timed_out = cancelled = False
    hproc, job, port = started.pi.hProcess, started.job, started.port
    leftovers = 0
    total = None
    try:
        while True:
            r = WaitForSingleObject(hproc, 200)
            _drain_port(port, triggered)
            if r == WAIT_OBJECT_0:
                break
            if poll:
                try:
                    poll()
                except BaseException:
                    cancelled = True
                    TerminateJobObject(job, 1)
                    raise
            if policy.wall_time_s is not None and time.monotonic() - start > policy.wall_time_s:
                timed_out = True
                triggered.add("wall_time")
                TerminateJobObject(job, 1)
                WaitForSingleObject(hproc, 10000)
                break
        _drain_port(port, triggered)
        acc = _accounting(job)
        grace = time.monotonic() + 1.0   # console hosts / launcher shims exit just after the main process
        while acc.ActiveProcesses and not timed_out and time.monotonic() < grace:
            time.sleep(0.05)
            acc = _accounting(job)
        total = acc.TotalProcesses
        if acc.ActiveProcesses and (policy.kill_leftovers_on_exit or timed_out):
            leftovers = acc.ActiveProcesses
            TerminateJobObject(job, 1)
            if not timed_out:
                triggered.add("leftover_processes_killed")
        code = None if timed_out else _exit_code(hproc)
    finally:
        if cancelled:
            TerminateJobObject(job, 1)
        po.join(10); pe.join(10)
        for f in (fout, ferr):
            try:
                f.close()
            except OSError:
                pass
        _close(hproc); _close(port); _close(job)   # closing the job kills anything left (KILL_ON_JOB_CLOSE)
    if po.truncated:
        triggered.add("output_cap:stdout")
    if pe.truncated:
        triggered.add("output_cap:stderr")
    return RunResult(code, po.data, pe.data, timed_out, cancelled, time.monotonic() - start, po.truncated, pe.truncated, sorted(triggered),
                     policy.limits_dict(), _isolation_info(policy, started.mode, started.applied, started.downgrade),
                     processes_total=total, leftovers_killed=leftovers, argv=list(argv))


class _WinProcess(SandboxedProcess):
    def __init__(self, started: _Started, policy: IsolationPolicy):
        self._s = started
        self.pid = started.pi.dwProcessId
        self.isolation = _isolation_info(policy, started.mode, started.applied, started.downgrade)
        self._code: int | None = None
        self._closed = False

    def poll(self):
        if self._closed:
            return self._code
        if WaitForSingleObject(self._s.pi.hProcess, 0) == WAIT_OBJECT_0:
            self._code = _exit_code(self._s.pi.hProcess)
            return self._code
        return None

    def wait(self, timeout=None):
        if self._closed:
            return self._code
        WaitForSingleObject(self._s.pi.hProcess, 0xFFFFFFFF if timeout is None else int(timeout * 1000))
        return self.poll()

    def kill_tree(self):
        if self._closed:
            return
        TerminateJobObject(self._s.job, 1)
        WaitForSingleObject(self._s.pi.hProcess, 10000)
        self._code = _exit_code(self._s.pi.hProcess)
        _close(self._s.pi.hProcess); _close(self._s.port); _close(self._s.job)
        self._closed = True

    def __del__(self):  # job handle close kills the tree (KILL_ON_JOB_CLOSE)
        try:
            if not self._closed:
                _close(self._s.pi.hProcess); _close(self._s.port); _close(self._s.job)
        except Exception:
            pass


def spawn(argv, *, work: Path, cwd: Path, policy: IsolationPolicy, env: dict[str, str]) -> SandboxedProcess:
    started = _create(argv, cwd=cwd, env=env, policy=policy, std=None, console="new")
    return _WinProcess(started, policy)


def appcontainer_profile_exists() -> bool:
    import winreg
    sid, text = _appcontainer_sid()
    FreeSid(sid)
    key = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer\Mappings" + "\\" + text
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, key))
        return True
    except OSError:
        return False


def probe(mode: str) -> dict[str, Any]:
    """Can this host create a process in ``mode``? Runs cmd.exe /c exit 0 isolated (no files touched outside a temp dir).
    The AppContainer probe never creates the per-user profile; it reports that it would be created on first use."""
    import tempfile
    if mode == "appcontainer" and not appcontainer_profile_exists():
        return {"available": "on_first_use", "note": f"per-user AppContainer profile {APPCONTAINER_NAME!r} is created the first time this mode is used"}
    from .sandbox import build_env, prepare_work_dir
    try:
        with tempfile.TemporaryDirectory(prefix="rs-iso-probe-") as td:
            pol = IsolationPolicy(integrity=mode, downgrade_reason="probe" if mode == "medium" else None, wall_time_s=15)
            work = Path(td)
            prepare_work_dir(work, pol)
            env = build_env(work, policy=pol)
            res = run([env.get("ComSpec") or "cmd.exe", "/d", "/c", "exit 0"], work=work, cwd=work, policy=pol, env=env, stdin=None, poll=None)
            return {"available": res.returncode == 0, "exit_code": res.returncode}
    except Exception as e:  # noqa: BLE001 - reported to the user
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
