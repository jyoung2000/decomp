"""Rebuild Studio plugin for Cutter.

Cutter loads this package by name from its ``plugins/python`` directory and calls :func:`create_cutter_plugin`
(rizinorg/cutter ``src/plugins/PluginManager.cpp`` ``loadPythonPlugin``).

* ``client`` - pure Python 3 (stdlib only): pairing, controller API, binary-to-module mapping, evidence views, feedback.
  Importable and testable without Cutter.
* ``plugin`` - the Cutter/Qt layer. ``cutter`` and PySide are imported lazily, only when Cutter asks for the plugin.

Importing this package never imports ``cutter`` or Qt.
"""
from __future__ import annotations

__version__ = "0.1.0"


def create_cutter_plugin():
    """Entry point Cutter calls. Raises ``plugin.CutterUnavailable`` with a clear message when run outside Cutter."""
    from . import plugin
    return plugin.make_plugin()
