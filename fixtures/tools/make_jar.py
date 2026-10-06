#!/usr/bin/env python3
"""Deterministic jar writer: META-INF/MANIFEST.MF first, then every *.class in sorted order, fixed 1980-01-01 timestamps,
STORED (no deflate) so the bytes do not depend on the zlib version.

usage: make_jar.py <classes_dir> <out.jar> [--main-class X] [--title T] [--version V]
"""
import argparse
import os
import zipfile

STAMP = (1980, 1, 1, 0, 0, 0)


def add(zf, name, data):
    zi = zipfile.ZipInfo(name, STAMP)
    zi.compress_type = zipfile.ZIP_STORED
    zi.external_attr = 0o644 << 16
    zi.create_system = 3
    zf.writestr(zi, data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("classes")
    ap.add_argument("out")
    ap.add_argument("--main-class")
    ap.add_argument("--title")
    ap.add_argument("--version")
    a = ap.parse_args()
    manifest = "Manifest-Version: 1.0\r\n"
    if a.main_class:
        manifest += f"Main-Class: {a.main_class}\r\n"
    if a.title:
        manifest += f"Implementation-Title: {a.title}\r\n"
    if a.version:
        manifest += f"Implementation-Version: {a.version}\r\n"
    manifest += "\r\n"
    names = []
    for dp, dns, fns in os.walk(a.classes):
        dns.sort()
        for fn in fns:
            if fn.endswith(".class"):
                names.append(os.path.relpath(os.path.join(dp, fn), a.classes).replace(os.sep, "/"))
    names.sort()
    with zipfile.ZipFile(a.out, "w") as zf:
        add(zf, "META-INF/MANIFEST.MF", manifest.encode())
        for n in names:
            with open(os.path.join(a.classes, n), "rb") as f:
                add(zf, n, f.read())
    print(f"wrote {a.out}: {len(names)} classes")


if __name__ == "__main__":
    main()
