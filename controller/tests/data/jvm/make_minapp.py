#!/usr/bin/env python3
"""Rebuild minapp.apk (a minimal unsigned Android package used by tests/test_jvm.py) with the Android SDK build-tools.

Needs ANDROID_HOME (build-tools + platforms/android-34) and a JDK 17. The checked-in minapp.apk was produced by this script on
Windows 11 with build-tools 34.0.0; the APK is a test sample only (not a fixture with an oracle; it is never executed).
"""
import os, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SDK = Path(os.environ["ANDROID_HOME"])
BT = SDK / "build-tools" / os.environ.get("BUILD_TOOLS", "34.0.0")
JAR = SDK / "platforms" / "android-34" / "android.jar"
exe = ".exe" if os.name == "nt" else ""

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "classes").mkdir()
    srcs = [str(p) for p in (HERE / "minapp_src").rglob("*.java")]
    subprocess.run(["javac", "--release", "8", "-g:none", "-cp", str(JAR), "-d", str(td / "classes"), *srcs], check=True)
    cls = [str(p) for p in (td / "classes").rglob("*.class")]
    d8 = str(BT / ("d8.bat" if os.name == "nt" else "d8"))
    subprocess.run([d8, "--release", "--min-api", "24", "--lib", str(JAR), "--output", str(td), *cls], check=True)
    subprocess.run([str(BT / f"aapt2{exe}"), "link", "--manifest", str(HERE / "minapp_src" / "AndroidManifest.xml"), "-I", str(JAR),
                    "-o", str(td / "base.apk")], check=True)
    out = HERE / "minapp.apk"
    with zipfile.ZipFile(td / "base.apk") as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in zin.infolist():
            zout.writestr(i.filename, zin.read(i))
        zout.write(td / "classes.dex", "classes.dex")
    print("wrote", out, out.stat().st_size, "bytes")
