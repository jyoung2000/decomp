"""Hermes computer-use bridge: pairing, MCP registration, diagnostics, agent sessions and direct cua-driver automation.

Two modes, always labelled on every record: ``hermes_agent_session`` (the user's Hermes CLI, model-driven) and
``cua_driver_direct`` (deterministic recipes straight to cua-driver; NOT a Hermes integration). See docs/HERMES.md.
"""
from .bridge import (BRIDGE_SCHEMA, DEFAULT_TASK_ACTIONS, MODE_AGENT, MODE_DIRECT, ActionRecord, DirectDriverSession,  # noqa: F401
                     HermesBridge, HermesBridgeError, HermesTask, Recipe, Scope, ScopeViolation, StreamParser, SystemProbe,
                     TaskResult, diagnose_text, merge_mcp_server, run_recipe)
from .protocol import PROTOCOL_VERSION, ProtocolError, UnsupportedAction  # noqa: F401
