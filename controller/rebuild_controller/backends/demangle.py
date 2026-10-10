"""Symbol demangling helpers (MSVC, Itanium C++, Rust legacy + v0) on top of rizin's demanglers (``iD <lang> <sym>``).

``guess_lang`` picks the scheme from the mangled prefix; ``qualified_name`` turns a full demangled signature into the
qualified function name a reader (and the benchmark truth, which comes from PDB procedure names) uses:
``public: void __cdecl std::bad_alloc::constructor(void) __ptr64`` -> ``std::bad_alloc::bad_alloc``;
``std[2a1f8e54e2930f4]::rt::lang_start_internal::h0123456789abcdef`` -> ``std::rt::lang_start_internal``.
Swift is not covered: rizin 0.9.1 ships no Swift demangler.
"""
from __future__ import annotations

import re

_RUST_HASH = re.compile(r"::h[0-9a-f]{16}$")
_RUST_DISAMB = re.compile(r"\[[0-9a-f]{1,16}\]")
_MSVC_PREFIX = re.compile(r"^(?:(?:public|private|protected): )?(?:(?:static|virtual|__thiscall|__cdecl|__stdcall|__fastcall|__vectorcall) )*")
_CALLCONV = re.compile(r"\b(?:__cdecl|__stdcall|__fastcall|__thiscall|__vectorcall|__clrcall)\s+")


def guess_lang(sym: str) -> str | None:
    if sym.startswith("?"):
        return "msvc"
    if sym.startswith("_R") or (sym.startswith("_ZN") and re.search(r"17h[0-9a-f]{16}E$", sym)):
        return "rust"
    if sym.startswith(("_Z", "__Z")):
        return "c++"
    return None


def _strip_args(s: str) -> str:
    """Drop the outermost trailing parameter list (and anything after it), respecting nested <> and ()."""
    depth_angle = 0
    for i, ch in enumerate(s):
        if ch == "<":
            depth_angle += 1
        elif ch == ">":
            depth_angle = max(0, depth_angle - 1)
        elif ch == "(" and depth_angle == 0:
            # "operator()" is a name, not the parameter list
            if s[max(0, i - 8):i].endswith("operator") and s[i + 1:i + 2] == ")":
                continue
            return s[:i]
    return s


def _drop_return_type(s: str) -> str:
    """'void * __ptr64 std::foo' -> 'std::foo': the qualified name is the last space-separated token outside <>."""
    depth = 0
    cut = 0
    for i, ch in enumerate(s):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == " " and depth == 0:
            # keep "operator new", "operator delete[]" etc. together
            if s[i + 1:].startswith(("new", "delete")) and s[:i].endswith("operator"):
                continue
            cut = i + 1
    out = s[cut:]
    if out.startswith(("new", "delete")) and s[:cut].rstrip().endswith("operator"):
        out = "operator " + out
    return out


def qualified_name(lang: str | None, demangled: str) -> str:
    d = (demangled or "").strip()
    if not d:
        return d
    if lang == "rust":
        d = _RUST_HASH.sub("", d)
        return strip_inherent(_RUST_DISAMB.sub("", d), " as ")
    if lang in ("msvc", "c++"):
        d = _CALLCONV.sub("", _MSVC_PREFIX.sub("", d))
        d = _strip_args(d).strip()
        d = _drop_return_type(d).strip()
        d = tidy(d)
        parts = d.split("::")
        if len(parts) >= 2:
            cls = re.sub(r"<.*>$", "", parts[-2])
            if parts[-1] == "constructor":
                parts[-1] = cls
            elif parts[-1] == "destructor":
                parts[-1] = "~" + cls
        return "::".join(parts)
    return d


_ELAB = re.compile(r"(?<=[<,\s(])(?:class|struct|enum|union)\s+")
_VECTORCALL = re.compile(r"@@\d+$")


def strip_inherent(name: str, as_token: str) -> str:
    """Rust v0 writes inherent methods as ``<std::net::UdpSocket>::bind``; PDBs and readers use ``std::net::UdpSocket::bind``.
    Trait impls (``<X as Trait>::m``) are kept as they are."""
    if not name.startswith("<"):
        return name
    depth = 0
    for i, ch in enumerate(name):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                inner, rest = name[1:i], name[i + 1:]
                if rest.startswith("::") and as_token not in inner:
                    return inner + rest
                return name
    return name


def tidy(name: str) -> str:
    """Drop MSVC's elaborated-type keywords inside template arguments (``<class std::x>`` -> ``<std::x>``), ``__ptr64`` and
    the ``@@<n>`` vectorcall decoration, so names read like (and compare equal to) PDB procedure names."""
    n = _ELAB.sub("", name.replace(" __ptr64", ""))
    n = _VECTORCALL.sub("", n)
    return re.sub(r"\s*,\s*", ",", n)


def pat_safe(name: str, limit: int = 240) -> str:
    """A name usable as one whitespace-free token in a FLIRT .pat line."""
    return re.sub(r"\s+", "_", name)[:limit]
