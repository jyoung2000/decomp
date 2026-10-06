"""Offscreen smoke test of the Qt layer (plugin.py) with a STAND-IN ``cutter`` module. Not a substitute for running in Cutter.

Needs PySide6 (or PySide2) in the interpreter you run it with; it does not need Cutter.

    QT_QPA_PLATFORM=offscreen python clients/cutter/dev/qt_smoke.py [path/to/binary] [--data-dir DIR]

It builds the plugin against a fake ``cutter`` (CutterPlugin base, CutterDockWidget = QDockWidget, a core() with a ``seekChanged``
signal, cmdj('ij')), creates the dock, fires a seek, waits for the first view and prints what the dock shows. With a running
Rebuild Studio whose case contains the binary, the view is the real briefing/decompile evidence; otherwise it shows the
"unavailable"/"not mapped" state, which is also worth seeing.
"""
import argparse
import os
import sys
import time
import types
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("binary", nargs="?", default=str(Path(__file__).resolve().parents[3] / "fixtures" / "pecli" / "original" / "pecli.exe"))
ap.add_argument("--data-dir")
ap.add_argument("--address", default="0x140001190")
ap.add_argument("--baddr", default="0x140000000")
args = ap.parse_args()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if args.data_dir:
    os.environ["REBUILD_STUDIO_DATA"] = args.data_dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtWidgets  # type: ignore

app = QtWidgets.QApplication([])
ADDR = int(args.address, 16)


class Core(QtCore.QObject):
    seekChanged = QtCore.Signal("qulonglong", object)

    def getOffset(self):
        return ADDR


core = Core()
cut = types.ModuleType("cutter")


class CutterDockWidget(QtWidgets.QDockWidget):
    def __init__(self, parent):
        super().__init__(parent)


class CutterPlugin:
    def __init__(self):
        pass


cut.CutterDockWidget, cut.CutterPlugin = CutterDockWidget, CutterPlugin
cut.core = lambda: core
cut.cmdj = lambda c: {"core": {"file": args.binary, "format": "pe64"}, "bin": {"baddr": int(args.baddr, 16)}}
cut.cmd = lambda c: hex(ADDR)
cut.message = lambda t: print("[cutter.message]", t)
sys.modules["cutter"] = cut

import rebuild_studio_cutter as pkg  # noqa: E402

plug = pkg.create_cutter_plugin()
main = QtWidgets.QMainWindow()
main.addPluginDockWidget = lambda w: main.addDockWidget(QtCore.Qt.RightDockWidgetArea, w)
plug.setupInterface(main)
dock = plug.dock
deadline = time.time() + 15
while time.time() < deadline and dock._view is None:
    app.processEvents()
    time.sleep(0.05)
print("status   :", dock._status.text())
print("briefing :\n" + dock._briefing.toPlainText())
print("decompile:\n" + dock._decomp.toPlainText()[:400])
plug.terminate()
