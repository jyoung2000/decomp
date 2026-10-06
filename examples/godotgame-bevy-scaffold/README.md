# godotgame → Rust + Bevy (recovery verified, reconstruction NOT claimed)

Pipeline run on 2026-10-06 over `fixtures/godotgame/original/game.pck` with original execution **off** (no Godot engine binary on this host; none is redistributed).
- Recovery: GDRE tools 2.7.0 headless recovered the project (engine 4.3.0 detected; all 7 entries extracted; byte-identical to the fixture's source as checked by the fixture build, not by the app).
- Reconstruction: a buildable **Rust + Bevy 0.18 scaffold** (compiled in ~303 s) plus the recovered Godot project copied under `source/recovered/` as intermediate evidence. No gameplay feature is implemented or claimed; the feature ledger is empty because no scenarios were declared and runtime observation was not authorised.
- Verification: none possible (no baseline). The parity report says full parity = NO with 0 features; M-COMPARE is blocked with the reason.
This is the honest outcome for an engine profile without a runnable original; a real remake needs declared scenarios, an authorised engine run (or recorded captures) and a model route or external client for the Bevy port.
