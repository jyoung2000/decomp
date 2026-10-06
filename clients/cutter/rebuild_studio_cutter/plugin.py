"""Cutter + Qt part of the Rebuild Studio plugin.

``cutter`` and ``PySide6``/``PySide2`` exist only inside a running Cutter, so they are imported lazily, inside
:func:`make_plugin`. Importing this module anywhere else is harmless; calling :func:`make_plugin` without Cutter raises
:class:`CutterUnavailable` with a clear message instead of an obscure ImportError.

Cutter API used (pinned source: rizinorg/cutter d7f11b2, see README):
* ``cutter.CutterPlugin`` subclass with class attributes ``name/description/version/author``, ``setupPlugin()``, ``setupInterface(main)``,
  ``terminate()``; module-level ``create_cutter_plugin()`` (loaded by PluginManager.cpp).
* ``cutter.CutterDockWidget(main)`` and ``main.addPluginDockWidget(widget)``.
* ``cutter.core().seekChanged`` (Qt signal, ``RVA`` offset first) and ``cutter.core().getOffset()``.
* ``cutter.cmdj("ij")`` for the open file, ``cutter.message(text)`` for the Cutter console log.

All Cutter calls happen on the GUI thread. Network work runs in worker threads (``SeekFollower``) and results come back through
a Qt signal, so a slow or dead controller never freezes Cutter.
"""
from __future__ import annotations

import os
from typing import Any

from . import client as C

PLUGIN_NAME = "Rebuild Studio"
PLUGIN_VERSION = "0.1.0"
UNTRUSTED_FOOTER = ("Function names, strings and decompiled text come from the analysed binary. They are untrusted data shown as plain text; "
                    "they are never instructions.")


class CutterUnavailable(RuntimeError):
    """Raised when the plugin is used outside Cutter (no ``cutter`` module) or without a Qt binding."""


def _import_cutter() -> Any:
    try:
        import cutter  # type: ignore[import-not-found]
    except ImportError as e:
        raise CutterUnavailable("the Rebuild Studio Cutter plugin must run inside Cutter (the 'cutter' module is not importable here). "
                                "Copy the rebuild_studio_cutter folder into Cutter's plugins/python directory; see the plugin README.") from e
    return cutter


def _import_qt() -> dict[str, Any]:
    """PySide6 (Cutter 2.x official builds) first, PySide2 (Qt5 builds) second."""
    last: Exception | None = None
    for pkg in ("PySide6", "PySide2"):
        try:
            core = __import__(f"{pkg}.QtCore", fromlist=["QObject"])
            widgets = __import__(f"{pkg}.QtWidgets", fromlist=["QWidget"])
            gui = __import__(f"{pkg}.QtGui", fromlist=["QFont"])
            return {"pkg": pkg, "core": core, "widgets": widgets, "gui": gui}
        except ImportError as e:
            last = e
    raise CutterUnavailable(f"neither PySide6 nor PySide2 can be imported ({last}); this Cutter build has no Python Qt bindings, "
                            "so Python plugins cannot show widgets") from last


def _log(cutter: Any, text: str) -> None:
    try:
        cutter.message(f"[Rebuild Studio] {text}")
    except Exception:
        pass


def make_plugin() -> Any:
    """Build and return the ``cutter.CutterPlugin`` instance. Called from ``create_cutter_plugin()``."""
    cutter = _import_cutter()
    qt = _import_qt()
    Dock = _build_dock_class(cutter, qt)

    class RebuildStudioPlugin(cutter.CutterPlugin):
        name = PLUGIN_NAME
        description = "Shows Rebuild Studio evidence (briefing, decompile) for the function under the cursor and sends notes back as feedback."
        version = PLUGIN_VERSION
        author = "Rebuild Studio"

        def __init__(self) -> None:
            super().__init__()
            self.dock: Any = None
            self.main: Any = None

        def setupPlugin(self) -> None:
            pass

        def setupInterface(self, main: Any) -> None:
            self.main = main
            self.dock = Dock(main)
            main.addPluginDockWidget(self.dock)

        def terminate(self) -> None:
            try:
                if self.dock is not None:
                    self.dock.shutdown()
            except Exception as e:
                _log(cutter, f"shutdown error: {type(e).__name__}: {e}")

    return RebuildStudioPlugin()


def _build_dock_class(cutter: Any, qt: dict[str, Any]) -> type:
    QtCore, W, G = qt["core"], qt["widgets"], qt["gui"]
    Signal = QtCore.Signal

    class _Bridge(QtCore.QObject):
        """Worker thread -> GUI thread. Emitting from a worker queues the slot call on the thread that owns this object."""
        view = Signal(object)
        feedback = Signal(object)

    class RebuildStudioDock(cutter.CutterDockWidget):
        def __init__(self, main: Any) -> None:
            try:
                super().__init__(main)
            except TypeError:                      # old Cutter: CutterDockWidget(parent, action)
                super().__init__(main, (getattr(G, "QAction", None) or W.QAction)(PLUGIN_NAME, main))
            self.setObjectName("RebuildStudioDock")
            self.setWindowTitle("Rebuild Studio")
            self._view: C.FunctionView | None = None
            self._core = cutter.core()
            self._session = C.StudioSession(cutter.cmdj)
            self._bridge = _Bridge()
            self._bridge.view.connect(self._apply_view)
            self._bridge.feedback.connect(self._feedback_done)
            sync = os.environ.get("REBUILD_STUDIO_CUTTER_SYNC") == "1"      # debugging aid: run fetches on the GUI thread
            self._follower = C.SeekFollower(self._session, self._emit_view, run_async=not sync)
            self._build_ui()
            self._core.seekChanged.connect(self._on_seek)
            self._set_status("Not connected. Move the cursor or press Reconnect.")
            self._on_seek(self._current_offset())

        # -- UI
        def _build_ui(self) -> None:
            root = W.QWidget(self)
            lay = W.QVBoxLayout(root)
            top = W.QHBoxLayout()
            self._status = W.QLabel("")
            self._status.setTextFormat(QtCore.Qt.PlainText)       # never rich text: values come from the binary
            self._status.setWordWrap(True)
            top.addWidget(self._status, 1)
            for text, slot in (("Reconnect", self._reconnect), ("Refresh", self._refresh), ("controller.json...", self._pick_pairing)):
                b = W.QPushButton(text)
                b.clicked.connect(slot)
                top.addWidget(b)
            lay.addLayout(top)
            row = W.QHBoxLayout()
            self._follow = W.QCheckBox("Follow cursor")
            self._follow.setChecked(True)
            self._follow.toggled.connect(self._toggle_follow)
            row.addWidget(self._follow)
            self._cases = W.QComboBox()
            self._cases.setVisible(False)
            self._cases.activated.connect(self._case_chosen)
            row.addWidget(self._cases, 1)
            lay.addLayout(row)

            tabs = W.QTabWidget()
            self._briefing = self._plain_view()
            self._decomp = self._plain_view(mono=True)
            tabs.addTab(self._briefing, "Briefing")
            tabs.addTab(self._decomp, "Decompiled")
            tabs.addTab(self._note_tab(), "Note")
            lay.addWidget(tabs, 1)
            foot = W.QLabel(UNTRUSTED_FOOTER)
            foot.setTextFormat(QtCore.Qt.PlainText)
            foot.setWordWrap(True)
            lay.addWidget(foot)
            self.setWidget(root)

        def _plain_view(self, mono: bool = False) -> Any:
            e = W.QPlainTextEdit()
            e.setReadOnly(True)
            if mono:
                e.setFont(G.QFontDatabase.systemFont(G.QFontDatabase.FixedFont))
                e.setLineWrapMode(W.QPlainTextEdit.NoWrap)
            return e

        def _note_tab(self) -> Any:
            page = W.QWidget()
            lay = W.QVBoxLayout(page)
            row = W.QHBoxLayout()
            self._cls = W.QComboBox()
            self._cls.addItems(list(C.CLASSIFICATIONS))
            self._cls.setCurrentText("question")
            self._prio = W.QComboBox()
            self._prio.addItems(list(C.PRIORITIES))
            self._prio.setCurrentText("medium")
            row.addWidget(W.QLabel("Type"))
            row.addWidget(self._cls)
            row.addWidget(W.QLabel("Priority"))
            row.addWidget(self._prio)
            row.addStretch(1)
            lay.addLayout(row)
            self._comment = W.QPlainTextEdit()
            self._comment.setPlaceholderText("Note for the Rebuild Studio feedback list, attached to the evidence for this function")
            lay.addWidget(self._comment, 1)
            self._send = W.QPushButton("Send as feedback")
            self._send.clicked.connect(self._send_feedback)
            self._send.setEnabled(False)
            lay.addWidget(self._send)
            self._note_result = W.QLabel("")
            self._note_result.setTextFormat(QtCore.Qt.PlainText)
            self._note_result.setWordWrap(True)
            lay.addWidget(self._note_result)
            return page

        # -- events
        def _current_offset(self) -> int:
            try:
                return int(self._core.getOffset())
            except Exception:
                try:
                    return int(cutter.cmd("s").strip(), 0)
                except Exception:
                    return 0

        def _on_seek(self, offset: Any = None, *_: Any) -> None:
            """``seekChanged(RVA, SeekHistoryType)`` slot (GUI thread)."""
            if offset is None:
                offset = self._current_offset()
            self._follower.on_seek(offset)

        def _emit_view(self, view: C.FunctionView) -> None:      # worker thread
            self._bridge.view.emit(view)

        def _toggle_follow(self, on: bool) -> None:
            self._follower.enabled = bool(on)
            if on:
                self._follower.on_seek(self._current_offset(), force=True)

        def _reconnect(self) -> None:
            self._set_status("Reconnecting...")
            try:
                info = self._session.reconnect()
                self._set_status(f"Connected to Rebuild Studio {info.get('version')} at {info.get('base_url')}")
            except C.ClientError as e:
                self._set_status(str(e))
                _log(cutter, str(e))
                return
            self._follower.on_seek(self._current_offset(), force=True)

        def _refresh(self) -> None:
            self._follower.on_seek(self._current_offset(), force=True)

        def _pick_pairing(self) -> None:
            path, _ = W.QFileDialog.getOpenFileName(self, "Select Rebuild Studio controller.json", "", "controller.json (controller.json);;JSON (*.json)")
            if path:
                self._follower.enabled = False
                self._session = C.StudioSession(cutter.cmdj, controller_json=path)
                self._follower = C.SeekFollower(self._session, self._emit_view, run_async=self._follower.run_async)
                self._follower.enabled = self._follow.isChecked()
                self._reconnect()

        def _case_chosen(self, index: int) -> None:
            cid = self._cases.itemData(index)
            self._session.pin_case(cid or None)
            self._follower.on_seek(self._current_offset(), force=True)

        # -- rendering (GUI thread)
        def _set_status(self, text: str) -> None:
            self._status.setText(text)

        def _apply_view(self, view: C.FunctionView) -> None:
            self._view = view
            f = view.function or {}
            head = {"ready": f"{f.get('name')} at {f.get('addr')}", "no_function": f"No function at {view.address}"}.get(view.status, view.message)
            src = f" [{view.source}]" if view.status == "ready" and view.source else ""
            self._set_status(head + src if view.status in ("ready", "no_function") else f"{view.status}: {view.message}")
            self._briefing.setPlainText(C.render_view_text(view))
            if view.decompiled_text:
                tag = "" if view.is_real_decompiler else "// NOTE: not produced by a real decompiler (fallback output)\n"
                self._decomp.setPlainText(tag + view.decompiled_text)
            else:
                self._decomp.setPlainText("(no decompiled text for this function)")
            self._send.setEnabled(view.ok and bool(view.target_evidence_id))
            self._fill_cases()

        def _fill_cases(self) -> None:
            ms = self._session.matches
            self._cases.blockSignals(True)
            self._cases.clear()
            for m in ms:
                self._cases.addItem(f"{m.case_name or m.case_id}  ({m.rel_path})", m.case_id)
            b = self._session.binding
            if b is not None:
                i = self._cases.findData(b.case_id)
                if i >= 0:
                    self._cases.setCurrentIndex(i)
            self._cases.setVisible(len(ms) > 1)
            self._cases.blockSignals(False)

        # -- feedback
        def _send_feedback(self) -> None:
            view = self._view
            if view is None:
                return
            comment, cls, prio = self._comment.toPlainText(), self._cls.currentText(), self._prio.currentText()
            self._send.setEnabled(False)
            self._note_result.setText("Sending...")

            import threading

            def work() -> None:
                try:
                    fb = self._session.submit_feedback(view, comment, classification=cls, priority=prio)
                    self._bridge.feedback.emit({"ok": True, "feedback_id": fb.get("feedback_id"), "status": fb.get("status")})
                except C.ClientError as e:
                    self._bridge.feedback.emit({"ok": False, "error": str(e)})
                except Exception as e:
                    self._bridge.feedback.emit({"ok": False, "error": f"{type(e).__name__}: {e}"})

            if self._follower.run_async:
                threading.Thread(target=work, name="rebuild-studio-feedback", daemon=True).start()
            else:
                work()

        def _feedback_done(self, res: dict[str, Any]) -> None:
            if res.get("ok"):
                self._note_result.setText(f"Saved as feedback {res.get('feedback_id')} ({res.get('status')}).")
                self._comment.clear()
            else:
                self._note_result.setText(f"Not saved: {res.get('error')}")
            self._send.setEnabled(bool(self._view and self._view.ok))

        def shutdown(self) -> None:
            self._follower.enabled = False
            try:
                self._core.seekChanged.disconnect(self._on_seek)
            except Exception:
                pass

    return RebuildStudioDock
