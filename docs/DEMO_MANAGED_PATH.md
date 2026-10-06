# Demo: managed-code (.NET) path with an external model client

`examples/dotnetapp-rust-from-evidence/` records an end-to-end run of the managed path on the `dotnetapp` fixture (.NET 8 NotesApp):

- No-AI pipeline: inventory → ILSpy recovery (`recover_managed`, ilspycmd 9.1) → feature discovery (8 features / 9 scenarios) → scaffold r1 → compare **0/9** → M-IMPL blocked with a pointer to the task packet.
- An external client (Claude Code over the `rebuild-mcp` stdio server, `--toolset rebuild`) read only app evidence (feature list, task packet, ILSpy-recovered C#), proposed a dependency-free Rust project, and the verifier recorded **9/9 on the first attempt** (exit code, CRLF-normalised stdout/stderr, byte-exact state JSON). Delivery produced a parity report with full parity = YES.
- The fixture's `src/` and `expected/` were never read by the client (DR-8).
- Non-certifying: Linux host, frozen oracle baseline; the Windows build of the Rust source is a separate release gate.

See that directory's README for the evidence ids, attempt table and caveats. The native-code counterpart is `examples/pecli-rust-from-evidence/`.
