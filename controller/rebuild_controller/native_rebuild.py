"""R4: rebuild in the original language first.

For .NET input (target ``csharp``) and JVM input (target ``java``) the source recovered by ILSpy / CFR is the starting candidate:

    prepare (deterministic project file + known decompiler quirks) -> build in the sandbox -> error-driven deterministic repairs
    (missing using/import, compiler-reserved attributes) -> rebuild ... -> run the frozen scenarios (the verifier is the only judge)

Only when scenarios still fail (or the build still fails after the deterministic rules) AND the project's AI policy allows it is
the AI implement loop scheduled, starting from that candidate with the build log / mismatch digest as its first feedback, so the
model repairs the diff instead of re-imagining the program. Every rule that changed a file is recorded (evidence
``native_rebuild``, candidate meta ``deterministic_repairs``); every repair round is a new candidate revision (author
``deterministic``), so the history shows exactly what was changed by rule.
"""
from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .builders.java import release_for_class_major
from .jobs.runner import Cancelled, StageContext, StageError

NATIVE_TARGETS = {"csharp": ("dotnet",), "java": ("jvm",)}
TARGET_TITLE = {"csharp": "C#", "java": "Java"}
MAX_DETERMINISTIC_ROUNDS = 4
MAX_RESOURCE_BYTES = 64 * 1024 * 1024


def native_target_for(profile: str | None) -> str | None:
    """The original-language target for a detected profile (None when there is none)."""
    for t, profs in NATIVE_TARGETS.items():
        if profile in profs:
            return t
    return None


def unsupported_native(profile: str, target: str) -> str | None:
    if target not in NATIVE_TARGETS or profile in NATIVE_TARGETS[target] or profile in ("unknown", None):
        return None
    lang = TARGET_TITLE[target]
    if target == "java" and profile == "android":
        return ("the Java target rebuilds desktop Java programs (.jar); an Android app needs the Android SDK to build, which is not "
                "integrated. Choose Rust as a port")
    src = ".NET program" if target == "csharp" else "Java program (.jar)"
    return f"the {lang} target rebuilds a {src} from its recovered {lang}; this input is '{profile}'. Choose Rust (port) or Auto"


# ====================================================================================== C#: tables and rules
CSHARP_USINGS: dict[str, str] = {
    **{n: "System.Collections.Generic" for n in ("List", "Dictionary", "HashSet", "Queue", "Stack", "IEnumerable", "IList", "IDictionary",
                                                   "ICollection", "KeyValuePair", "SortedDictionary", "SortedSet", "LinkedList", "IReadOnlyList",
                                                   "IReadOnlyDictionary", "IReadOnlyCollection", "IEnumerator", "IComparer", "IEqualityComparer",
                                                   "Comparer", "EqualityComparer", "SortedList")},
    **{n: "System.IO" for n in ("File", "Directory", "Path", "Stream", "FileStream", "StreamReader", "StreamWriter", "TextReader", "TextWriter",
                                 "MemoryStream", "FileInfo", "DirectoryInfo", "IOException", "FileNotFoundException", "BinaryReader", "BinaryWriter",
                                 "FileMode", "FileAccess", "FileShare", "SearchOption", "StringWriter", "StringReader", "DirectoryNotFoundException")},
    **{n: "System.Text" for n in ("StringBuilder", "Encoding", "UTF8Encoding")},
    **{n: "System.Text.Json" for n in ("JsonSerializer", "JsonSerializerOptions", "JsonException", "JsonDocument", "JsonElement", "Utf8JsonWriter",
                                        "JsonNamingPolicy")},
    **{n: "System.Text.Json.Serialization" for n in ("JsonPropertyName", "JsonPropertyNameAttribute", "JsonIgnore", "JsonIgnoreAttribute",
                                                      "JsonConverter", "JsonStringEnumConverter", "JsonSerializable", "JsonSerializerContext")},
    **{n: "System.Text.RegularExpressions" for n in ("Regex", "Match", "MatchCollection", "RegexOptions", "Group")},
    **{n: "System.Linq" for n in ("Enumerable", "IGrouping", "ILookup", "IOrderedEnumerable")},
    **{n: "System.Threading.Tasks" for n in ("Task", "ValueTask", "Parallel", "TaskCompletionSource")},
    **{n: "System.Threading" for n in ("Thread", "CancellationToken", "CancellationTokenSource", "Interlocked", "Monitor", "SemaphoreSlim", "Volatile", "Timer")},
    **{n: "System.Globalization" for n in ("CultureInfo", "NumberStyles", "DateTimeStyles")},
    **{n: "System.Diagnostics" for n in ("Stopwatch", "Process", "ProcessStartInfo", "Debug", "Trace", "Debugger", "DebuggerDisplay", "DebuggerHidden",
                                          "DebuggerStepThrough", "DebuggerBrowsable", "DebuggerBrowsableState")},
    **{n: "System.Runtime.CompilerServices" for n in ("CompilerGenerated", "CompilerGeneratedAttribute", "IteratorStateMachine", "AsyncStateMachine",
                                                       "RuntimeHelpers", "CallerMemberName", "MethodImpl", "MethodImplOptions", "IsExternalInit")},
    **{n: "System.Runtime.InteropServices" for n in ("DllImport", "Marshal", "StructLayout", "LayoutKind", "CharSet", "SafeHandle")},
    **{n: "System.Reflection" for n in ("Assembly", "BindingFlags", "MethodInfo", "PropertyInfo", "FieldInfo", "DefaultMember")},
    **{n: "System.Collections" for n in ("ArrayList", "Hashtable", "IEnumerable_nongeneric")},
    **{n: "System.ComponentModel" for n in ("EditorBrowsable", "EditorBrowsableState", "INotifyPropertyChanged", "PropertyChangedEventArgs")},
    **{n: "System.Numerics" for n in ("BigInteger", "Complex", "Vector2", "Vector3", "Vector4", "Matrix4x4", "Quaternion")},
    **{n: "System.Net.Http" for n in ("HttpClient", "HttpResponseMessage", "HttpRequestMessage", "HttpMethod")},
    **{n: "System.Buffers" for n in ("ArrayPool",)},
}
LINQ_METHODS = {"Select", "Where", "ToList", "ToArray", "ToDictionary", "ToHashSet", "First", "FirstOrDefault", "Last", "LastOrDefault", "Any", "All",
                "Count", "Sum", "Min", "Max", "Average", "OrderBy", "OrderByDescending", "ThenBy", "ThenByDescending", "GroupBy", "Skip", "Take",
                "Distinct", "Concat", "Zip", "Aggregate", "SelectMany", "Single", "SingleOrDefault", "Contains", "Reverse", "SequenceEqual",
                "Cast", "OfType", "ElementAt", "DefaultIfEmpty", "Except", "Intersect", "Union", "AsEnumerable", "TakeWhile", "SkipWhile"}
# Attributes C# compilers emit themselves; ILSpy prints some of them, and csc refuses them in source (CS8335/CS1112).
RESERVED_ATTRIBUTES = ("RefSafetyRules", "Nullable", "NullableContext", "NullablePublicOnly", "IsReadOnly", "IsByRefLike", "IsUnmanaged",
                       "Embedded", "ScopedRef", "RequiresLocation", "ParamCollection", "Extension")
# runtimeconfig.json configProperties -> MSBuild property that regenerates them (the original's runtime behaviour, e.g. invariant culture)
RUNTIMECONFIG_PROPS = {"System.Globalization.Invariant": "InvariantGlobalization", "System.Globalization.PredefinedCulturesOnly": "PredefinedCulturesOnly",
                       "System.GC.Server": "ServerGarbageCollection", "System.GC.Concurrent": "ConcurrentGarbageCollection",
                       "System.GC.RetainVM": "RetainVMGarbageCollection", "System.Runtime.TieredCompilation": "TieredCompilation",
                       "System.Runtime.TieredPGO": "TieredPGO", "System.Globalization.UseNls": "UseSystemResourceKeys_unused",
                       "System.Threading.ThreadPool.MinThreads": "ThreadPoolMinThreads", "System.Threading.ThreadPool.MaxThreads": "ThreadPoolMaxThreads"}


def tfm_from_metadata(target_framework: str | None) -> str | None:
    """'.NETCoreApp,Version=v8.0' -> 'net8.0'; '.NETStandard,Version=v2.0' -> 'netstandard2.0'; '.NETFramework,Version=v4.7.2' -> 'net472'."""
    m = re.match(r"^\.(NETCoreApp|NETStandard|NETFramework),Version=v(\d+)\.(\d+)(?:\.(\d+))?", target_framework or "")
    if not m:
        return None
    kind, a, b, c = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    if kind == "NETCoreApp":
        return f"net{a}.{b}" if a >= 5 else f"netcoreapp{a}.{b}"
    if kind == "NETStandard":
        return f"netstandard{a}.{b}"
    return f"net{a}{b}{c or ''}"


def csproj_from_metadata(meta: dict[str, Any], runtimeconfig: dict[str, Any] | None = None, *, has_assembly_info: bool = True) -> str:
    """A project file built from the assembly's own metadata (used when the decompiler wrote none)."""
    asm = (meta.get("assembly") or {}).get("name") or "app"
    exe = not (meta.get("pe") or {}).get("is_dll", True) or bool((meta.get("pe") or {}).get("entry_point_token") not in (None, "0x00000000"))
    tfm = tfm_from_metadata(meta.get("target_framework")) or "net8.0"
    lines = ['<Project Sdk="Microsoft.NET.Sdk">', "  <PropertyGroup>", f"    <AssemblyName>{_xml(asm)}</AssemblyName>",
             f"    <OutputType>{'Exe' if exe else 'Library'}</OutputType>", f"    <TargetFramework>{tfm}</TargetFramework>",
             "    <LangVersion>latest</LangVersion>", "    <AllowUnsafeBlocks>True</AllowUnsafeBlocks>", "    <Nullable>annotations</Nullable>",
             "    <ImplicitUsings>disable</ImplicitUsings>"]
    if has_assembly_info:
        lines.append("    <GenerateAssemblyInfo>False</GenerateAssemblyInfo>")
    lines += [f"    <{p}>{v}</{p}>" for p, v in runtimeconfig_properties(runtimeconfig).items()]
    lines += ["  </PropertyGroup>", "</Project>", ""]
    return "\n".join(lines)


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def runtimeconfig_properties(rc: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    opts = (rc or {}).get("runtimeOptions") or {}
    for k, v in (opts.get("configProperties") or {}).items():
        prop = RUNTIMECONFIG_PROPS.get(k)
        if prop and not prop.endswith("_unused") and isinstance(v, (bool, int, str)):
            out[prop] = ("true" if v else "false") if isinstance(v, bool) else str(v)
    if isinstance(opts.get("rollForward"), str):
        out["RollForward"] = opts["rollForward"]
    return out


def fix_csproj(text: str, meta: dict[str, Any] | None, runtimeconfig: dict[str, Any] | None) -> tuple[str, list[str]]:
    """Known ILSpy project-file quirks + settings only the original's runtimeconfig.json still carries."""
    notes: list[str] = []
    m = re.search(r"<TargetFramework>\s*netcoreapp(\d+)\.(\d+)\s*</TargetFramework>", text)
    if m and int(m.group(1)) >= 5:
        text = text.replace(m.group(0), f"<TargetFramework>net{m.group(1)}.{m.group(2)}</TargetFramework>")
        notes.append(f"project file: target framework netcoreapp{m.group(1)}.{m.group(2)} -> net{m.group(1)}.{m.group(2)} (ILSpy writes the pre-.NET 5 moniker)")
    if "<TargetFramework>" not in text and "<TargetFrameworks>" not in text and meta:
        tfm = tfm_from_metadata(meta.get("target_framework"))
        if tfm:
            text = _add_props(text, {"TargetFramework": tfm})
            notes.append(f"project file: target framework {tfm} from the assembly's TargetFrameworkAttribute")
    add: dict[str, str] = {}
    if "<Nullable>" not in text:
        add["Nullable"] = "annotations"       # ILSpy keeps '?' annotations without a nullable context (CS8632 warnings)
    if "<ImplicitUsings>" not in text:
        add["ImplicitUsings"] = "disable"     # the recovered files carry their own using directives
    for prop, v in runtimeconfig_properties(runtimeconfig).items():
        if f"<{prop}>" not in text:
            add[prop] = v
    if add:
        text = _add_props(text, add)
        rc = [p for p in add if p not in ("Nullable", "ImplicitUsings")]
        if rc:
            notes.append(f"project file: {', '.join(f'{p}={add[p]}' for p in rc)} from the original's runtimeconfig.json")
    return text, notes


def _add_props(text: str, props: dict[str, str]) -> str:
    block = "  <PropertyGroup>\n" + "".join(f"    <{k}>{_xml(v)}</{k}>\n" for k, v in props.items()) + "  </PropertyGroup>\n"
    i = text.rfind("</Project>")
    return text[:i] + block + text[i:] if i >= 0 else text + block


_RESERVED_LINE = re.compile(r"^\s*\[\s*(?:module|assembly)\s*:\s*(?:System\.Runtime\.CompilerServices\.)?(" + "|".join(RESERVED_ATTRIBUTES) +
                            r")(?:Attribute)?\s*(?:\([^\]]*\))?\s*\]\s*\r?\n?", re.M)


def strip_reserved_attributes(text: str) -> tuple[str, list[str]]:
    """Remove assembly/module-level attributes the compiler reserves for itself (ILSpy prints ``[module: RefSafetyRules(11)]``)."""
    found = [m.group(1) for m in _RESERVED_LINE.finditer(text)]
    return (_RESERVED_LINE.sub("", text), found) if found else (text, [])


_CS_ERR = re.compile(r"^\s*(?P<file>[^\r\n]*?\.cs)\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+(?P<code>CS\d{4}):\s*(?P<msg>.*?)(?:\s+\[[^\]]*\.csproj\])?\s*$", re.M)


def parse_csharp_errors(log: str) -> list[dict[str, Any]]:
    seen, out = set(), []
    for m in _CS_ERR.finditer(log or ""):
        key = (m.group("file"), m.group("line"), m.group("col"), m.group("code"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"file": m.group("file").strip(), "line": int(m.group("line")), "col": int(m.group("col")), "code": m.group("code"), "msg": m.group("msg")})
    return out


def _rel(src: Path, reported: str) -> str | None:
    p = Path(reported)
    try:
        rel = p.resolve().relative_to(src.resolve()) if p.is_absolute() else Path(reported)
    except (ValueError, OSError):
        return None
    rel_s = rel.as_posix()
    return rel_s if (src / rel_s).is_file() and ".." not in Path(rel_s).parts else None


def csharp_type_index(src: Path) -> dict[str, str]:
    """Types declared in the candidate's own sources -> their namespace (for missing 'using' of a sibling namespace)."""
    idx: dict[str, str] = {}
    for p in src.rglob("*.cs"):
        if any(x in ("bin", "obj") for x in p.relative_to(src).parts):
            continue
        t = p.read_text("utf-8", "replace")
        ns = re.search(r"^\s*namespace\s+([\w.]+)\s*[;{]", t, re.M)
        if not ns:
            continue
        for m in re.finditer(r"\b(?:class|struct|interface|enum|record|delegate\s+\S+)\s+([A-Za-z_]\w*)", t):
            idx.setdefault(m.group(1), ns.group(1))
    return idx


def add_csharp_using(text: str, ns: str) -> str | None:
    if re.search(rf"^\s*using\s+{re.escape(ns)}\s*;", text, re.M):
        return None
    usings = list(re.finditer(r"^\s*(?:global\s+)?using\s+[\w.=\s]+;\s*$", text, re.M))
    line = f"using {ns};"
    if usings:
        end = usings[-1].end()
        return text[:end] + "\n" + line + text[end:]
    return line + "\n" + text


def _remove_attribute_at(text: str, line_no: int, col: int) -> str | None:
    lines = text.splitlines(keepends=True)
    if not 1 <= line_no <= len(lines):
        return None
    ln = lines[line_no - 1]
    i = min(max(col - 1, 0), len(ln))
    start = ln.rfind("[", 0, i + 1)
    if start < 0:
        return None
    depth, end = 0, -1
    for j in range(start, len(ln)):
        ch = ln[j]
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end < 0:
        return None
    inner = ln[start + 1:end]
    if re.sub(r"\([^)]*\)", "", inner).count(","):
        return None        # several attributes in one bracket: not a mechanical edit
    rest = ln[:start] + ln[end + 1:]
    lines[line_no - 1] = "" if not rest.strip() else rest
    return "".join(lines)


_IL_INS = re.compile(r"^\s*(IL_[0-9a-fA-F]{4}):\s*([a-z][\w.]*)(?:\s+(.*?))?\s*$")


def _il_methods(il: str) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """[(method name, [(label, opcode, operand)])] from an ``ilspycmd -il`` listing."""
    out = []
    for chunk in re.split(r"(?m)^\s*\.method\b", il)[1:]:
        head = chunk.split("{", 1)[0]
        names = re.findall(r"([A-Za-z_<][\w<>$`.]*)\s*(?:<[^>]*>)?\s*\(", head)
        if not names:
            continue
        body = chunk.split("{", 1)[1] if "{" in chunk else ""
        body = re.split(r"(?m)}\s*//\s*end of method", body)[0]
        ins = [(m.group(1), m.group(2), m.group(3) or "") for m in map(_IL_INS.match, body.splitlines()) if m]
        out.append((names[-1].split(".")[-1], ins))
    return out


def _ldloc(op: str, arg: str) -> str | None:
    if op.startswith("ldloc.") and op[6:].isdigit():
        return op[6:]
    if op in ("ldloc", "ldloc.s"):
        return arg.strip() or None
    return None


def il_null_shared_cases(il: str) -> dict[str, set[str]]:
    """Per method: string constants whose equality test jumps to the SAME place as the null test of the same local.

    ``if (c == null || c == "0" || c == "q") return;`` compiles to ``ldloc; brfalse L`` + ``ldloc; ldstr "0"; op_Equality; brtrue L`` + ...
    ILSpy 9.1 turns that chain into ``switch (c) { case "0": case "q": return; ...}`` and drops the null test, so a null (EOF) input falls
    into ``default``. The IL is the ground truth for adding the ``case null:`` back."""
    res: dict[str, set[str]] = {}
    for name, ins in _il_methods(il):
        null_t: dict[str, str] = {}
        eq: dict[str, dict[str, str]] = {}
        for i, (_lab, op, arg) in enumerate(ins):
            loc = _ldloc(op, arg)
            if loc is None or i + 1 >= len(ins):
                continue
            nop, narg = ins[i + 1][1], ins[i + 1][2].strip()
            nxt_label = lambda k: ins[k][0] if k < len(ins) else None   # noqa: E731
            if nop in ("brfalse", "brfalse.s"):
                null_t.setdefault(loc, narg)
            elif nop in ("brtrue", "brtrue.s"):
                lbl = nxt_label(i + 2)
                if lbl:
                    null_t.setdefault(loc, lbl)
            elif nop == "ldstr" and i + 3 < len(ins) and "System.String::op_Equality" in ins[i + 2][2]:
                try:
                    k = json.loads(narg)
                except ValueError:
                    continue
                bop, barg = ins[i + 3][1], ins[i + 3][2].strip()
                tgt = barg if bop in ("brtrue", "brtrue.s") else (nxt_label(i + 4) if bop in ("brfalse", "brfalse.s") else None)
                if tgt and isinstance(k, str):
                    eq.setdefault(loc, {})[k] = tgt
        shared = {k for loc, t in null_t.items() for k, lbl in (eq.get(loc) or {}).items() if lbl == t}
        if shared:
            res.setdefault(name, set()).update(shared)
    return res


def _method_body_span(text: str, method: str) -> list[tuple[int, int]]:
    """Brace spans of the bodies of methods called ``method`` (declarations, not calls)."""
    spans = []
    for m in re.finditer(rf"(?m)^[ \t]*(?:[\w<>\[\],?.]+[ \t]+)+{re.escape(method)}[ \t]*\([^;{{]*\)\s*(?:where[^{{]*)?\{{", text):
        start = m.end() - 1
        depth = 0
        for j in range(start, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    spans.append((start, j))
                    break
    return spans


def add_null_cases(text: str, method: str, consts: set[str]) -> tuple[str, int]:
    """Insert ``case null:`` before the first ``case "K":`` (K in consts) of a string switch in ``method`` that has no null case."""
    added = 0
    for start, end in reversed(_method_body_span(text, method)):
        body = text[start:end]
        new_body, pos = body, 0
        out = []
        for sw in re.finditer(r"switch\s*\(\s*\w+\s*\)\s*\{", body):
            # this switch's block
            depth, j0 = 0, sw.end() - 1
            j_end = None
            for j in range(j0, len(body)):
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        j_end = j
                        break
            if j_end is None:
                continue
            block = body[j0:j_end]
            if re.search(r"\bcase\s+null\s*:", block):
                continue
            first = None
            for c in re.finditer(r'(?m)^([ \t]*)case\s+("(?:[^"\\]|\\.)*")\s*:', block):
                try:
                    if json.loads(c.group(2)) in consts:
                        first = c
                        break
                except ValueError:
                    continue
            if first is None:
                continue
            ins_at = j0 + first.start()
            out.append((ins_at, f"{first.group(1)}case null:\n"))
        for at, s in reversed(out):
            new_body = new_body[:at] + s + new_body[at:]
            added += 1
        if out:
            text = text[:start] + new_body + text[end:]
    return text, added


def apply_il_switch_rule(ilspy, dll: Path, src: Path, types: list[dict[str, Any]], *, ctx: Any = None, max_types: int = 60) -> list[str]:
    """Known ILSpy quirk (null test dropped from a string switch), fixed only where the original IL proves the null case."""
    from .backends.ilspy import _expected_type_path
    notes: list[str] = []
    done = 0
    for t in types:
        if done >= max_types or t.get("nested"):
            continue
        rel = _expected_type_path(t["name"], t.get("namespace") or "")
        p = src / rel
        if not p.is_file():
            continue
        text = p.read_text("utf-8-sig", "replace")
        if not re.search(r"switch\s*\(", text) or 'case "' not in text:
            continue
        done += 1
        if ctx is not None:
            ctx.heartbeat()
        il = ilspy.il_listing(dll, t["name"], ctx=ctx)
        if not il:
            continue
        total = 0
        for method, consts in il_null_shared_cases(il).items():
            text, n = add_null_cases(text, method, consts)
            total += n
        if total:
            p.write_text(text, encoding="utf-8")
            notes.append(f"{rel}: restored {total} 'case null:' label(s) ILSpy dropped from string switches (the IL tests null and the "
                         f"constant with the same branch target)")
    return notes


def repair_csharp(src: Path, log: str) -> tuple[dict[str, str], list[str]]:
    """Error-driven, rule-based edits for one failed ``dotnet build``. Returns (changed files, notes); empty when no rule applies."""
    errors = parse_csharp_errors(log)
    texts: dict[str, str] = {}
    notes: list[str] = []
    idx: dict[str, str] | None = None

    def text_of(rel: str) -> str:
        if rel not in texts:
            texts[rel] = (src / rel).read_text("utf-8-sig", "replace")
        return texts[rel]
    changed: set[str] = set()
    # attribute removals bottom-up per file so earlier line numbers stay valid
    for e in sorted(errors, key=lambda e: (e["file"], -e["line"], -e["col"])):
        rel = _rel(src, e["file"])
        if rel is None:
            continue
        if e["code"] in ("CS8335", "CS1112") or (e["code"] == "CS0579" and re.search(r"^\s*\[\s*(assembly|module)\s*:", text_of(rel).splitlines()[e["line"] - 1] if e["line"] <= len(text_of(rel).splitlines()) else "")):
            new = _remove_attribute_at(text_of(rel), e["line"], e["col"])
            if new is not None and new != text_of(rel):
                texts[rel] = new; changed.add(rel)
                notes.append(f"{rel}:{e['line']}: removed a compiler-reserved/duplicate attribute ({e['code']})")
    for e in errors:
        rel = _rel(src, e["file"])
        if rel is None:
            continue
        ns = None
        if e["code"] in ("CS0246", "CS0103"):
            m = re.search(r"name '([A-Za-z_]\w*)(?:<[^']*>)?'", e["msg"])
            if m:
                name = m.group(1)
                ns = CSHARP_USINGS.get(name)
                if ns is None:
                    if idx is None:
                        idx = csharp_type_index(src)
                    ns = idx.get(name)
                    if ns and re.search(rf"^\s*namespace\s+{re.escape(ns)}\s*[;{{]", text_of(rel), re.M):
                        ns = None   # same namespace: the type is missing for another reason
        elif e["code"] == "CS1061":
            m = re.search(r"definition for '(\w+)'", e["msg"])
            if m and m.group(1) in LINQ_METHODS and "using directive" in e["msg"]:
                ns = "System.Linq"
        if ns:
            new = add_csharp_using(text_of(rel), ns)
            if new is not None:
                texts[rel] = new; changed.add(rel)
                notes.append(f"{rel}: added 'using {ns};' ({e['code']} {e['msg'][:80]})")
    return {r: texts[r] for r in sorted(changed)}, notes


# ====================================================================================== Java: tables and rules
JAVA_IMPORTS: dict[str, str] = {
    **{n: f"java.util.{n}" for n in ("List", "ArrayList", "LinkedList", "Map", "HashMap", "LinkedHashMap", "TreeMap", "Set", "HashSet", "LinkedHashSet",
                                       "TreeSet", "Collection", "Collections", "Arrays", "Iterator", "Optional", "Objects", "Scanner", "Comparator",
                                       "Deque", "ArrayDeque", "Queue", "PriorityQueue", "Locale", "UUID", "Random", "StringJoiner", "NoSuchElementException",
                                       "SortedMap", "NavigableMap", "SortedSet", "Properties", "OptionalInt", "OptionalLong", "BitSet", "EnumMap", "EnumSet")},
    **{n: f"java.util.function.{n}" for n in ("Function", "BiFunction", "Supplier", "Consumer", "BiConsumer", "Predicate", "BiPredicate",
                                               "UnaryOperator", "BinaryOperator", "IntFunction", "ToIntFunction", "ToLongFunction")},
    **{n: f"java.util.stream.{n}" for n in ("Collectors", "Stream", "IntStream", "LongStream")},
    **{n: f"java.io.{n}" for n in ("File", "IOException", "UncheckedIOException", "InputStream", "OutputStream", "Reader", "Writer", "BufferedReader",
                                     "BufferedWriter", "InputStreamReader", "OutputStreamWriter", "PrintStream", "PrintWriter", "FileReader", "FileWriter",
                                     "FileInputStream", "FileOutputStream", "ByteArrayOutputStream", "ByteArrayInputStream", "StringWriter",
                                     "Serializable", "Closeable", "EOFException", "FileNotFoundException")},
    **{n: f"java.nio.file.{n}" for n in ("Files", "Path", "Paths", "StandardOpenOption", "NoSuchFileException", "StandardCopyOption",
                                           "InvalidPathException", "AccessDeniedException", "DirectoryStream")},
    **{n: f"java.nio.charset.{n}" for n in ("StandardCharsets", "Charset")},
    **{n: f"java.math.{n}" for n in ("BigDecimal", "BigInteger", "RoundingMode", "MathContext")},
    **{n: f"java.time.{n}" for n in ("Instant", "Duration", "LocalDate", "LocalDateTime", "LocalTime", "ZoneId", "ZonedDateTime")},
    **{n: f"java.util.regex.{n}" for n in ("Pattern", "Matcher")},
    **{n: f"java.util.concurrent.{n}" for n in ("TimeUnit", "ConcurrentHashMap", "ExecutorService", "Executors", "Future", "CompletableFuture")},
    **{n: f"java.util.concurrent.atomic.{n}" for n in ("AtomicInteger", "AtomicLong", "AtomicBoolean", "AtomicReference")},
    "DecimalFormat": "java.text.DecimalFormat", "SimpleDateFormat": "java.text.SimpleDateFormat", "MessageFormat": "java.text.MessageFormat",
    "NumberFormat": "java.text.NumberFormat", "ParseException": "java.text.ParseException",
}


def parse_javac_errors(log: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lines = (log or "").splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"^(.+?\.java):(\d+): error: (.*)$", ln.strip())
        if not m:
            continue
        e: dict[str, Any] = {"file": m.group(1), "line": int(m.group(2)), "msg": m.group(3)}
        for nxt in lines[i + 1:i + 6]:
            s = re.match(r"^\s*symbol:\s+(class|variable|method)\s+(\S+)", nxt)
            if s:
                e["symbol_kind"], e["symbol"] = s.group(1), s.group(2)
                break
            if re.match(r"^(.+?\.java):(\d+): error:", nxt.strip()):
                break
        out.append(e)
    return out


def java_type_index(src_root: Path) -> dict[str, str]:
    """Top-level types of the candidate's own sources -> fully qualified name."""
    idx: dict[str, str] = {}
    for p in src_root.rglob("*.java"):
        t = p.read_text("utf-8", "replace")
        pk = re.search(r"^\s*package\s+([\w.]+)\s*;", t, re.M)
        idx.setdefault(p.stem, f"{pk.group(1)}.{p.stem}" if pk else p.stem)
    return idx


def add_java_import(text: str, fqcn: str) -> str | None:
    if re.search(rf"^\s*import\s+{re.escape(fqcn)}\s*;", text, re.M):
        return None
    pkg = fqcn.rsplit(".", 1)[0] if "." in fqcn else ""
    if pkg and re.search(rf"^\s*import\s+{re.escape(pkg)}\.\*\s*;", text, re.M):
        return None
    line = f"import {fqcn};"
    imports = list(re.finditer(r"^\s*import\s+[\w.*]+\s*;\s*$", text, re.M))
    if imports:
        end = imports[-1].end()
        return text[:end] + "\n" + line + text[end:]
    pk = re.search(r"^\s*package\s+[\w.]+\s*;\s*$", text, re.M)
    if pk:
        return text[:pk.end()] + "\n\n" + line + text[pk.end():]
    return line + "\n" + text


def repair_java(src: Path, log: str, source_root: str = "src") -> tuple[dict[str, str], list[str]]:
    errors = parse_javac_errors(log)
    root = src / source_root
    texts: dict[str, str] = {}
    notes: list[str] = []
    idx: dict[str, str] | None = None
    changed: set[str] = set()
    for e in errors:
        rel = _rel(src, e["file"])
        if rel is None:
            continue
        if rel not in texts:
            texts[rel] = (src / rel).read_text("utf-8", "replace")
        text = texts[rel]
        if e["msg"].startswith("cannot find symbol") and e.get("symbol_kind") == "class":
            name = e["symbol"]
            if "$" in name:
                # CFR quirk: an inner class written with its binary name Outer$Inner
                outer, inner = name.split("$", 1)
                if re.fullmatch(r"[A-Za-z_]\w*", inner or "") and re.search(rf"\b{re.escape(name)}\b", text):
                    texts[rel] = text.replace(name, f"{outer}.{inner}")
                    changed.add(rel)
                    notes.append(f"{rel}:{e['line']}: '{name}' -> '{outer}.{inner}' (decompiler wrote the binary name of a nested class)")
                continue
            fq = JAVA_IMPORTS.get(name)
            if fq is None:
                if idx is None:
                    idx = java_type_index(root) if root.is_dir() else {}
                fq = idx.get(name)
                own_pkg = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
                if fq and "." in fq and own_pkg and fq.rsplit(".", 1)[0] == own_pkg.group(1):
                    fq = None
            if fq and "." in fq:
                new = add_java_import(text, fq)
                if new is not None:
                    texts[rel] = new; changed.add(rel)
                    notes.append(f"{rel}: added 'import {fq};' (cannot find symbol: class {name})")
    return {r: texts[r] for r in sorted(changed)}, notes


def repair_from_build_log(target: str, src: Path, log: str) -> tuple[dict[str, str], list[str]]:
    if target == "csharp":
        return repair_csharp(src, log)
    if target == "java":
        proj = {}
        try:
            proj = json.loads((src / "rebuild-java.json").read_text("utf-8"))
        except (OSError, ValueError):
            pass
        return repair_java(src, log, str(proj.get("source_root") or "src"))
    return {}, []


# ====================================================================================== candidate preparation
def _module_reports(st, case_id: str, key: str) -> list[dict[str, Any]]:
    out = []
    for ev in st.cases.list_evidence(case_id, kind="module_report"):
        body = st.cases.evidence_body(ev["evidence_id"]) or {}
        if isinstance(body, dict) and body.get(key) and body.get("out_dir") and Path(body["out_dir"]).is_dir():
            out.append(body)
    return out


def _copy_tree(src: Path, dest: Path, *, skip_top: tuple[str, ...] = ()) -> int:
    n = 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if not p.is_file() or p.is_symlink() or (rel.parts and rel.parts[0] in skip_top):
            continue
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, d)
        n += 1
    return n


def prepare_csharp(st, case: dict[str, Any], src: Path, ctx: Any = None) -> dict[str, Any]:
    """Copy the recovered C# of the entry assembly (libraries as referenced projects) and apply the project-level rules."""
    reps = _module_reports(st, case["case_id"], "metadata")
    if not reps:
        raise StageError("no recovered C# to start from", blocker="the .NET recovery (ILSpy) produced no source; open Tools and install ILSpy, then Resume")

    def is_exe(r: dict[str, Any]) -> bool:
        pe = (r.get("metadata") or {}).get("pe") or {}
        return not pe.get("is_dll", True)
    reps.sort(key=lambda r: (not is_exe(r), (r["metadata"].get("assembly") or {}).get("name") or ""))
    main = reps[0]
    notes: list[str] = []
    projects: list[dict[str, Any]] = []
    for i, r in enumerate(reps):
        meta = r.get("metadata") or {}
        asm = (meta.get("assembly") or {}).get("name") or f"module{i}"
        dest = src if i == 0 else src / "libs" / asm
        n = _copy_tree(Path(r["out_dir"]), dest, skip_top=("bin", "obj"))
        mod = st.cases.get_module(r["module_id"]) if r.get("module_id") else None
        rc = None
        if mod:
            rcp = Path(case["source_root"]) / Path(mod["rel_path"]).with_suffix(".runtimeconfig.json")
            if rcp.is_file():
                try:
                    rc = json.loads(rcp.read_text("utf-8-sig"))
                except ValueError:
                    rc = None
        projs = sorted(dest.glob("*.csproj"))
        if not projs:
            p = dest / f"{asm}.csproj"
            p.write_text(csproj_from_metadata(meta, rc, has_assembly_info=(dest / "Properties" / "AssemblyInfo.cs").is_file()), encoding="utf-8")
            notes.append(f"{p.relative_to(src).as_posix()}: project file generated from the assembly's metadata ({tfm_from_metadata(meta.get('target_framework')) or 'net8.0'})")
            projs = [p]
        else:
            text = projs[0].read_text("utf-8-sig")
            new, n2 = fix_csproj(text, meta, rc)
            if new != text:
                projs[0].write_text(new, encoding="utf-8")
            notes += [f"{projs[0].relative_to(src).as_posix()}: {x.split(': ', 1)[-1]}" for x in n2]
        for cs in dest.rglob("*.cs"):
            if any(x in ("bin", "obj", "libs") for x in cs.relative_to(dest).parts[:-1]):
                continue
            t = cs.read_text("utf-8-sig", "replace")
            new, found = strip_reserved_attributes(t)
            if found:
                cs.write_text(new, encoding="utf-8")
                notes.append(f"{cs.relative_to(src).as_posix()}: removed compiler-reserved attribute(s) {', '.join(sorted(set(found)))} (ILSpy prints them; csc refuses them)")
        if mod:
            try:
                ilspy = st.registry.get("ilspy")
            except Exception:  # noqa: BLE001 - no ILSpy: the rule is skipped (a build/scenario failure still shows the problem)
                ilspy = None
            types = (meta.get("types") or {}).get("items") if isinstance(meta.get("types"), dict) else (meta.get("types") or [])
            if ilspy is not None and hasattr(ilspy, "il_listing"):
                for nt in apply_il_switch_rule(ilspy, Path(case["source_root"]) / mod["rel_path"], dest, list(types or []), ctx=ctx):
                    notes.append(nt if i == 0 else f"libs/{asm}/{nt}")
        projects.append({"assembly": asm, "project": projs[0].relative_to(src).as_posix(), "files": n, "module_id": r.get("module_id")})
    # libraries the entry assembly references become project references
    main_proj = src / projects[0]["project"]
    libs = {p["assembly"]: p for p in projects[1:]}
    refs = {x.get("name") for x in (main.get("metadata") or {}).get("references") or []}
    wanted = [p for a, p in libs.items() if a in refs]
    if wanted:
        t = main_proj.read_text("utf-8-sig")
        for p in wanted:
            t = re.sub(rf'\s*<Reference Include="{re.escape(p["assembly"])}"[^>]*?(/>|>.*?</Reference>)', "", t, flags=re.S)
        block = "  <ItemGroup>\n" + "".join(f'    <ProjectReference Include="{_xml(p["project"])}" />\n' for p in wanted) + "  </ItemGroup>\n"
        i = t.rfind("</Project>")
        main_proj.write_text(t[:i] + block + t[i:], encoding="utf-8")
        notes.append(f"{projects[0]['project']}: {len(wanted)} recovered librar{'y' if len(wanted) == 1 else 'ies'} referenced as project(s)")
    if len(list(src.glob("*.csproj"))) > 1:
        (src / ".rebuild-main-project").write_text(main_proj.name, encoding="utf-8")
    return {"projects": projects, "main_project": projects[0]["project"], "assembly": projects[0]["assembly"], "notes": notes}


def prepare_java(st, case: dict[str, Any], src: Path) -> dict[str, Any]:
    reps = _module_reports(st, case["case_id"], "inspect")
    reps = [r for r in reps if ((r.get("inspect") or {}).get("inspect") or {}).get("format") in ("jar", "class", None) and r.get("decompile")]
    if not reps:
        raise StageError("no recovered Java to start from", blocker="the Java recovery (CFR) produced no source; open Tools and install CFR, then Resume")
    reps.sort(key=lambda r: (not ((r.get("inspect") or {}).get("inspect") or {}).get("main_class")))
    main = reps[0]
    ins = (main.get("inspect") or {}).get("inspect") or {}
    notes: list[str] = []
    n = 0
    for r in reps:
        n += _copy_tree(Path(r["out_dir"]), src / "src", skip_top=())
    for p in list((src / "src").rglob("*")):
        if p.is_file() and p.suffix != ".java":
            p.unlink()        # decompiler side files (summary.txt) are not sources
    majors = [int(k) for k in (ins.get("class_file_versions") or {}) if str(k).isdigit()]
    major = max(majors) if majors else 61
    release = release_for_class_major(major)
    if release is None:
        raise StageError(f"class files are version {major} (newer than Java 21)", blocker="the pinned JDK 21 cannot compile for this Java version; choose Rust (port)")
    # resources: every non-class entry of the original jar(s) except the manifest and signatures, size-bounded
    res_files = 0
    mod = st.cases.get_module(main["module_id"]) if main.get("module_id") else None
    if mod and str(mod["rel_path"]).lower().endswith(".jar"):
        jar = Path(case["source_root"]) / mod["rel_path"]
        total = 0
        try:
            with zipfile.ZipFile(jar) as z:
                for zi in z.infolist():
                    name = zi.filename
                    up = name.upper()
                    if zi.is_dir() or name.endswith(".class") or up == "META-INF/MANIFEST.MF" or re.match(r"^META-INF/[^/]+\.(SF|RSA|DSA|EC)$", up):
                        continue
                    parts = Path(name).parts
                    if name.startswith(("/", "\\")) or ".." in parts or ":" in name:
                        continue
                    total += zi.file_size
                    if total > MAX_RESOURCE_BYTES:
                        notes.append("resources: stopped copying at the 64 MB limit")
                        break
                    d = src / "resources" / name
                    d.parent.mkdir(parents=True, exist_ok=True)
                    d.write_bytes(z.read(zi))
                    res_files += 1
        except (OSError, zipfile.BadZipFile) as e:
            notes.append(f"resources: the original jar could not be read ({e})")
    manifest = {k: v for k, v in (ins.get("manifest") or {}).items() if k not in ("Manifest-Version", "Main-Class", "Created-By")
                and not k.startswith(("Class-Path", "Launcher-Agent", "Premain", "Agent"))}
    jar_name = Path(mod["rel_path"]).name if mod else "app.jar"
    proj = {"main_class": ins.get("main_class"), "release": release, "jar": jar_name, "manifest": manifest, "source_root": "src", "resource_root": "resources",
            "class_file_major": major}
    (src / "rebuild-java.json").write_text(json.dumps(proj, indent=2) + "\n", encoding="utf-8")
    notes.append(f"rebuild-java.json: main class {proj['main_class']}, javac --release {release} (class files version {major}), jar {jar_name}")
    if res_files:
        notes.append(f"resources/: {res_files} non-class file(s) copied from the original jar")
    return {"main_class": proj["main_class"], "release": release, "java_files": n, "resources": res_files, "notes": notes}


def create_native_candidate(st, case: dict[str, Any], target: str, plan_rev: int, profile: str, ctx: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    cand = st.candidates.create(case["case_id"], target_language=target, output_type=case["output_type"], plan_revision=plan_rev,
                                meta={"origin": "native_recovered", "profile": profile})
    src = Path(cand["source_dir"])
    prep = prepare_csharp(st, case, src, ctx) if target == "csharp" else prepare_java(st, case, src)
    lang = TARGET_TITLE[target]
    (src / "REBUILD_README.md").write_text(
        f"# {case['name']} ({lang}) — rebuilt from the recovered {lang}\n\n"
        f"The source here was recovered from the original program by a decompiler ({'ILSpy' if target == 'csharp' else 'CFR'}) and is rebuilt "
        f"as-is in its original language. Deterministic fixes applied before the first build:\n\n"
        + "".join(f"- {n}\n" for n in prep["notes"]) + "\nWhether it behaves like the original is decided only by the scenario comparison "
        "(reports/parity-report.md).\n", encoding="utf-8")
    meta = {**cand["meta"], "deterministic_repairs": list(prep["notes"]), "prepared": {k: v for k, v in prep.items() if k != "notes"}}
    st.db.update("candidates", "candidate_id", cand["candidate_id"], {"meta": meta})
    return st.candidates.get(cand["candidate_id"]), prep


# ====================================================================================== the stage
def stage_native_rebuild(ctx: StageContext) -> dict[str, Any]:
    from .implement import LoopPolicy, mismatch_digest, route_status
    from .stages import build_candidate_impl, compare_candidate_impl
    st = ctx.services["studio"]
    case = st.cases.get_case(ctx.job.case_id)
    case_id = case["case_id"]
    cur = ctx.job.inputs["candidate_id"]
    target = st.candidates.get(cur)["target_language"]
    lang = TARGET_TITLE.get(target, target)
    has_baseline = bool(st.cases.list_evidence(case_id, kind="baseline"))
    rounds: list[dict[str, Any]] = []
    built, last_log = False, ""
    for r in range(MAX_DETERMINISTIC_ROUNDS + 1):
        ctx.heartbeat(force=True)
        try:
            build_candidate_impl(ctx, cur)
            built = True
            rounds.append({"round": r, "candidate_id": cur, "build": "built"})
            break
        except Cancelled:
            raise
        except StageError as e:
            if e.blocker and "not installed" in str(e).lower():
                raise                       # toolchain missing: the job blocks with the install action
            last_log = str(e)
            rnd: dict[str, Any] = {"round": r, "candidate_id": cur, "build": "failed", "log_tail": last_log[-3000:]}
            rounds.append(rnd)
            if r >= MAX_DETERMINISTIC_ROUNDS:
                break
            files, notes = repair_from_build_log(target, Path(st.candidates.get(cur)["source_dir"]), last_log)
            if not files:
                rnd["repairs"] = []
                ctx.log(f"The recovered {lang} does not build and no deterministic rule applies to the errors", "warn")
                break
            new = st.candidates.propose(case_id, files, note=f"deterministic repair round {r + 1}: " + "; ".join(notes)[:1500], author="deterministic",
                                        base_candidate=cur, plan_revision=st.plan.current_revision(case_id))
            prev_meta = st.candidates.get(cur)["meta"]
            st.db.update("candidates", "candidate_id", new["candidate_id"], {"meta": {**new["meta"], "origin": "native_recovered", "profile": prev_meta.get("profile"),
                                                                                    "deterministic_repairs": list(prev_meta.get("deterministic_repairs") or []) + notes}})
            rnd["repairs"] = notes
            rnd["next_candidate"] = new["candidate_id"]
            ctx.log(f"Deterministic repair round {r + 1}: {len(notes)} rule-based fix(es) ({notes[0][:120]}{'…' if len(notes) > 1 else ''}); rebuilding")
            cur = new["candidate_id"]
    rep = None
    verified = False
    if built and has_baseline:
        rep = compare_candidate_impl(ctx, cur)
        s = rep["summary"]
        verified = st.candidates.get(cur)["verification"] == "verified"
        ctx.log(f"Verifier: {s['passed']} of {s['scenarios']} scenarios match the original with the recovered {lang}"
                + (" — no AI was needed" if verified else ""), "info" if verified else "warn")
    for c in st.candidates.list(case_id):
        meta = dict(c["meta"])
        if bool(meta.get("final")) != (c["candidate_id"] == cur):
            meta["final"] = c["candidate_id"] == cur
            st.db.update("candidates", "candidate_id", c["candidate_id"], {"meta": meta})
    summary = (rep or {}).get("summary") or {}
    result: dict[str, Any] = {"final_candidate": cur, "target": target, "built": built, "verified": verified, "rounds": rounds,
                              "deterministic_repairs": st.candidates.get(cur)["meta"].get("deterministic_repairs") or [],
                              "scenarios": summary.get("scenarios"), "passed": summary.get("passed"), "ai_used": False,
                              "verification_report": (rep or {}).get("evidence_id")}
    ev = st.cases.add_evidence(case_id, "native_rebuild", f"Native-language rebuild ({lang})", body=result, inputs={"candidate": cur, "target": target},
                               meta={"verified": verified, "built": built, "passed": summary.get("passed"), "scenarios": summary.get("scenarios")},
                               producer="native_rebuild")
    result["evidence_id"] = ev["evidence_id"]
    impl, fix = st.plan.milestone_id(case_id, "M-IMPL"), st.plan.milestone_id(case_id, "M-FIX")
    for f in st.ledger.list(case_id):
        if f["impl_status"] in ("planned", "in_progress"):
            st.ledger.set_impl(f["feature_id"], "runnable" if built else "blocked")
    if built:
        st.plan.update_item(impl, status="completed", blockers=[], files=[st.candidates.get(cur)["source_dir"]])
    pol = LoopPolicy.from_case(case)
    need_ai = (not built) or (has_baseline and not verified)
    ai_ok, ai_msg = False, None
    if need_ai and pol.ai_enabled:
        rs = route_status(st, pol, "repair")
        if not rs["ok"]:
            rs = route_status(st, pol)
        ai_ok, ai_msg = rs["ok"], rs.get("message")
    if need_ai and ai_ok:
        feedback = (mismatch_digest(st, case_id, cur) if built else
                    {"kind": "build_failed", "build_log": last_log[-6000:], "instruction": f"The recovered {lang} does not compile; fix exactly these errors."})
        feedback["instruction"] = (f"This {lang} was recovered from the original program by a decompiler and is the starting point. "
                                   + ("It builds, but these scenarios differ from the original. " if built else "It does not compile yet. ")
                                   + "Change only what is needed and return the full content of every file you change.")
        from .reconstruct import _task_packet
        packet = _task_packet(st, case, target, (st.candidates.get(cur)["meta"] or {}).get("profile") or "unknown")
        packet["excerpts"] = []          # the CURRENT FILES are the recovered source; no second copy
        packet["note"] = f"The current files are the {lang} recovered from the original; repair them, do not rewrite the program."
        pev = st.cases.add_evidence(case_id, "ai_task_packet", f"Repair packet ({lang}, from the recovered source)", body=packet,
                                    inputs={"candidate": cur, "target": target}, meta={"bytes": len(json.dumps(packet, default=str)), "untrusted": True})
        loop = st.jobs.create(case_id, "implement_loop", f"AI repair of the recovered {lang} (up to {pol.max_attempts} attempts)",
                              {"candidate_id": cur, "packet": pev["evidence_id"], "start": "native", "initial_feedback": feedback,
                               "start_passed": int(summary.get("passed") or 0)},
                              depends_on=[ctx.job.job_id], milestone_id="M-FIX", max_attempts=3)
        st.plan.link_job(fix, loop.job_id)
        d = st.jobs.create(case_id, "deliver", "Publish source/dist/evidence/reports", {"candidate_from_job": loop.job_id}, depends_on=[loop.job_id],
                           milestone_id="M-DELIVER", max_attempts=1)
        st.plan.link_job(st.plan.milestone_id(case_id, "M-DELIVER"), d.job_id)
        ctx.log(f"Scheduled AI repair of what still fails (up to {pol.max_attempts} attempts), starting from the recovered {lang}")
        result.update(ai_scheduled=True, implement_job=loop.job_id)
        return result
    if verified:
        st.plan.update_item(fix, status="completed", blockers=[])
    elif need_ai:
        why = ai_msg if (pol.ai_enabled and ai_msg) else "AI is off for this project (policy 'No AI')"
        what = (f"{summary.get('failed', 0) + summary.get('errors', 0)} scenario(s) still differ" if built else f"the recovered {lang} does not compile")
        st.plan.update_item(fix, status="blocked", blockers=[f"{what} after the deterministic repairs; {why}. Next: review Comparisons, or allow AI "
                                                             f"repairs (AI policy) so a model fixes only what fails, starting from this {lang}."])
        if not built:
            st.plan.update_item(impl, status="blocked", blockers=[f"the recovered {lang} does not compile; see the build log evidence"])
    if not has_baseline:
        st.plan.update_item(st.plan.milestone_id(case_id, "M-COMPARE"), status="blocked", blockers=["no baseline: enable original execution or provide scenarios"])
    d = st.jobs.create(case_id, "deliver", "Publish source/dist/evidence/reports", {"candidate_id": cur}, depends_on=[ctx.job.job_id],
                       milestone_id="M-DELIVER", max_attempts=1)
    st.plan.link_job(st.plan.milestone_id(case_id, "M-DELIVER"), d.job_id)
    st.plan.revise(case_id, f"native-language rebuild ({lang}): " + ("verified without AI" if verified else ("built" if built else "build failed")))
    return result
