#!/usr/bin/env python3
"""Write <out> listing every file under <root>: rel path, size, sha256 (sorted, deterministic).

usage: make_manifest.py <fixture_id> <root_dir> <out.json>
"""
import hashlib, json, os, sys


def main():
    fid, root, out = sys.argv[1:4]
    files = []
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            files.append({"path": rel, "size": os.path.getsize(p), "sha256": h.hexdigest()})
    files.sort(key=lambda e: e["path"])
    doc = {"fixture": fid, "root": os.path.basename(os.path.normpath(root)),
           "file_count": len(files), "total_bytes": sum(e["size"] for e in files), "files": files}
    with open(out, "w", newline="\n") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")


if __name__ == "__main__":
    main()
