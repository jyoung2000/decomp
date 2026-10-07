"""Secret storage, redaction and subprocess environment isolation.

Storage
-------
* Windows (os.name == "nt"): values are encrypted with DPAPI (CryptProtectData, current-user scope) before they touch disk.
  NOTE: the DPAPI path is exercised only on a Windows host; on Linux CI it is covered by a structural test only
  (see docs/PROVIDERS.md, "Windows gates").
* Everything else: a 0600 file under ``<data_dir>/secrets``. This is NOT OS-backed encryption. The store emits a
  ``SecretStoreWarning`` (and ``SecretStore.warning``) so the UI can say so plainly.

Redaction
---------
Every secret that is stored, read or registered is added to a process-wide registry. ``redact(text)`` masks registered
secrets plus well-known key shapes (sk-..., sk-ant-..., AIza..., Bearer ..., key=...). All logging, events and error
messages that can carry provider text go through ``redact``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets as _stdlib_secrets
import threading
import warnings
from pathlib import Path
from typing import Any, Iterable, Mapping

log = logging.getLogger("rebuild.secrets")

REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 6  # shorter values cannot be masked without mangling ordinary text


class SecretStoreWarning(UserWarning):
    """The secret store is not OS-backed."""


class SecretError(Exception):
    pass


# --------------------------------------------------------------------------------------- redaction registry
_REG_LOCK = threading.Lock()
_REGISTRY: set[str] = set()

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"sk-or-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), REDACTED),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer " + REDACTED),
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:basic|digest|token|negotiate|ntlm)\s+[^\s\"',;}]{6,}"), r"\1" + REDACTED),
    (re.compile(r"(?i)([?&](?:key|api_key|apikey|access_token)=)[^&\s\"']+"), r"\1" + REDACTED),
    (re.compile(r"(?i)((?:x-api-key|x-goog-api-key|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])[^\s\"',;}]{6,}"),
     r"\1" + REDACTED),
]

SENSITIVE_KEYS = {"authorization", "x-api-key", "x-goog-api-key", "api-key", "api_key", "apikey", "proxy-authorization",
                  "secret", "token", "access_token", "password", "cookie", "set-cookie"}


def register_secret(value: str | None) -> None:
    if value and len(value) >= MIN_SECRET_LEN:
        with _REG_LOCK:
            _REGISTRY.add(value)


def unregister_secret(value: str | None) -> None:
    if value:
        with _REG_LOCK:
            _REGISTRY.discard(value)


def registered_secret_count() -> int:
    with _REG_LOCK:
        return len(_REGISTRY)


def redact(text: Any) -> str:
    """Mask every registered secret and known key shape. Always returns ``str``."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    with _REG_LOCK:
        secrets_sorted = sorted(_REGISTRY, key=len, reverse=True)
    for sec in secrets_sorted:
        if sec in s:
            s = s.replace(sec, REDACTED)
        # also catch the JSON-escaped / url-quoted spelling of the same value
        esc = json.dumps(sec)[1:-1]
        if esc != sec and esc in s:
            s = s.replace(esc, REDACTED)
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings; mask values under sensitive keys."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, Mapping):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in SENSITIVE_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


class RedactingFilter(logging.Filter):
    """Attach to any logger/handler that might see provider text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # never break logging
            pass
        return True


def install_redacting_filter(logger: logging.Logger) -> None:
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())


# --------------------------------------------------------------------------------------- env isolation
_ENV_ALLOW = {
    "PATH", "PATHEXT", "HOME", "USER", "USERNAME", "LOGNAME", "SHELL", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "TZ", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    # the vendor CLIs' own config-home overrides (point at the *user's* login, never at ours)
    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME",
}
_ENV_DENY = re.compile(r"(?i)(API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|^JEV_|^REBUILD_STUDIO_)")


def isolated_env(extra: Mapping[str, str] | None = None, *, base: Mapping[str, str] | None = None,
                 allow: Iterable[str] = ()) -> dict[str, str]:
    """Minimal environment for provider/vendor-CLI subprocesses.

    Only an allow-list of neutral variables passes. Anything that looks like a key/token/secret is dropped even if
    allow-listed, so a subscription-login CLI cannot silently fall back to a BYOK key from this process, and our own
    keys (JEV_API_KEY, provider keys) are never inherited. ``extra`` is applied last and is the only way to inject
    a variable deliberately.
    """
    src = os.environ if base is None else base
    allowed = _ENV_ALLOW | set(allow)
    env = {k: v for k, v in src.items() if k in allowed and not _ENV_DENY.search(k)}
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# --------------------------------------------------------------------------------------- backends
class _FileBackend:
    name = "file"
    os_backed = False

    def protect(self, data: bytes) -> bytes:
        return data

    def unprotect(self, data: bytes) -> bytes:
        return data


class _DpapiBackend:
    name = "dpapi"
    os_backed = True
    _ENTROPY = b"rebuild-studio/v1"

    def _blob_cls(self):
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        return DATA_BLOB

    def _call(self, fn_name: str, data: bytes) -> bytes:
        import ctypes
        DATA_BLOB = self._blob_cls()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        in_buf = ctypes.create_string_buffer(data, len(data))
        ent_buf = ctypes.create_string_buffer(self._ENTROPY, len(self._ENTROPY))
        blob_in = DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
        blob_ent = DATA_BLOB(len(self._ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        CRYPTPROTECT_UI_FORBIDDEN = 0x1
        fn = getattr(crypt32, fn_name)
        if fn_name == "CryptProtectData":
            ok = fn(ctypes.byref(blob_in), "RebuildStudio", ctypes.byref(blob_ent), None, None,
                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        else:
            ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None,
                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise SecretError(f"{fn_name} failed: {ctypes.WinError(ctypes.get_last_error())}")  # type: ignore[attr-defined]
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)

    def protect(self, data: bytes) -> bytes:
        return self._call("CryptProtectData", data)

    def unprotect(self, data: bytes) -> bytes:
        return self._call("CryptUnprotectData", data)


def _default_backend():
    return _DpapiBackend() if os.name == "nt" else _FileBackend()


# --------------------------------------------------------------------------------------- store
class SecretStore:
    """Opaque-reference secret store: ``put(value) -> ref``; the DB only ever holds ``ref``."""

    FILE_NAME = "secrets.json"

    def __init__(self, data_dir: Path | str, *, backend: Any = None):
        self.dir = Path(data_dir) / "secrets"
        self.path = self.dir / self.FILE_NAME
        self._backends = {"file": _FileBackend()}
        self._active = backend or _default_backend()
        self._backends[self._active.name] = self._active
        self._lock = threading.RLock()
        self.warning: str | None = None
        if not self._active.os_backed:
            self.warning = ("Secrets are stored in a 0600 file under the data directory. This is NOT OS-backed "
                            "encryption: anyone who can read your user profile can read these keys.")
            warnings.warn(SecretStoreWarning(self.warning), stacklevel=2)
            log.warning(self.warning)
        self._register_all()

    # -- properties
    @property
    def backend_name(self) -> str:
        return self._active.name

    @property
    def os_backed(self) -> bool:
        return bool(self._active.os_backed)

    # -- file io
    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "entries": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data.get("entries"), dict):
                raise ValueError("bad shape")
            return data
        except (OSError, ValueError) as e:
            raise SecretError(f"secret file unreadable ({type(e).__name__}); refusing to overwrite it") from e

    def _write(self, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        tmp = self.dir / f".{self.FILE_NAME}.{_stdlib_secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _register_all(self) -> None:
        try:
            for ref in self.refs():
                try:
                    register_secret(self.get(ref))
                except SecretError:
                    pass
        except SecretError:
            pass

    # -- api
    def put(self, value: str, *, ref: str | None = None) -> str:
        if not value:
            raise SecretError("empty secret")
        with self._lock:
            data = self._read()
            ref = ref or "secret:" + _stdlib_secrets.token_hex(8)
            blob = self._active.protect(value.encode("utf-8"))
            data["entries"][ref] = {"b": self._active.name, "v": base64.b64encode(blob).decode("ascii")}
            self._write(data)
        register_secret(value)
        return ref

    def get(self, ref: str | None) -> str | None:
        if not ref:
            return None
        with self._lock:
            ent = self._read()["entries"].get(ref)
        if ent is None:
            return None
        backend = self._backends.get(ent.get("b"))
        if backend is None:
            raise SecretError(f"secret {ref} was written by backend '{ent.get('b')}' which is unavailable on this host")
        try:
            value = backend.unprotect(base64.b64decode(ent["v"])).decode("utf-8")
        except SecretError:
            raise
        except Exception as e:
            raise SecretError(f"could not decrypt {ref}: {type(e).__name__}") from e
        register_secret(value)
        return value

    def delete(self, ref: str | None) -> bool:
        if not ref:
            return False
        with self._lock:
            data = self._read()
            existed = ref in data["entries"]
            if existed:
                try:
                    unregister_secret(self.get(ref))
                except SecretError:
                    pass
                del data["entries"][ref]
                self._write(data)
        return existed

    def refs(self) -> list[str]:
        with self._lock:
            return sorted(self._read()["entries"].keys())

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend_name, "os_backed": self.os_backed, "warning": self.warning,
                "path": str(self.path), "count": len(self.refs())}
