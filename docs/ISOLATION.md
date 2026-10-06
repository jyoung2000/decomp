# Process isolation for untrusted programs

Rebuild Studio runs three kinds of code it does not trust:

| What | Where | Who wrote it |
| --- | --- | --- |
| The **original** program (capturing baseline behaviour) | `comparators/cli.py` `run_steps(role="original")`, called by `stages.stage_capture_original` | unknown third party |
| **Candidate** remakes (verification, previews) | `comparators/cli.py` `compare_cli_scenario`, `previews.py` (native previews) | AI-generated |
| **Build scripts** (`build.rs`, proc-macros) | `builders/rust.py` (`cargo build`) | AI-generated |
| Web pages (original or candidate) | `comparators/web.py` (node + Chromium harness) | third party / AI-generated |

`builders/web.py` copies files and runs nothing. Static analysis (inventory, decompilers, source analysis) never runs the
user's program and needs no consent.

All of these go through `rebuild_controller/sandbox.py` (Windows implementation in `_sandbox_win.py`). This is
**damage limitation, not a security boundary**. The scenario work folder is a scratch folder for comparing files; it is
not, by itself, a protection of any kind. If the program might be actively malicious, use a disposable VM (see the end of
this page).

Every run records what was applied and what fired. Each step result of `run_steps` and each CLI comparison carries:

- `isolation`: platform, mode, integrity, Job Object limits applied, env policy, network status, filesystem statement,
  and `downgrade` (why isolation is weaker than the default, or `null`);
- `limits`: the configured caps (wall time, process/job memory, process count, stdout/stderr bytes, UI restrictions);
- `triggered`: which ones fired: `wall_time`, `process_memory`, `job_memory`, `active_processes`, `output_cap:stdout`,
  `output_cap:stderr`, `leftover_processes_killed`, (POSIX) `cpu_time`, `file_size`;
- `stdout_truncated` / `stderr_truncated`, `timed_out`, `duration_s`.

## Consent for the original program

Running the user's ORIGINAL program requires a recorded, per-case permission:

- Stored in the case's `launch_profile`: `allow_original_execution` (bool, default `false`),
  `original_execution_consent_at` (ISO timestamp), `original_execution_consent_via` (`create_case` / `api` / ...),
  `original_execution_consent_history` (every grant/revoke, last 50).
- `GET /cases/{case_id}/consent/original-execution` returns `{allowed, at, via, history}`.
- `PUT /cases/{case_id}/consent/original-execution` with `{"allow": true|false, "note": "..."}` grants/revokes and
  emits a `case.consent` event. Granting also sets `execute_original=true`, so the pipeline schedules baseline capture.
- A case created with `launch_profile.execute_original=true` (the user ticked "run the original" in the new-project
  form) records that as consent with `via="create_case"`; `allow_original_execution: false` in the same request overrides it.
- Without consent, `run_steps(role="original")` raises `OriginalExecutionNotPermitted`, and the `capture_original` job
  fails with a blocker. The message starts with **"Original execution needs your permission: "** and explains that the
  network is not blocked and the program can still read your files.
- A fixture/baseline file (`launch_profile.baseline_file`) needs no consent: nothing is executed.
- `GET /isolation` reports what this host offers (probed: low integrity, AppContainer, network default).

## Windows (native, non-elevated)

### Default mode: `low` (Job Object + Low integrity + scrubbed environment)

Guaranteed, and covered by tests on Windows 11 (see `controller/tests/test_sandbox.py`):

- **Whole-tree containment.** The process is created `CREATE_SUSPENDED`, assigned to a fresh Job Object, then resumed.
  Children cannot start outside the job (no `BREAKAWAY_OK`). The job has `KILL_ON_JOB_CLOSE`.
  Timeout, cancellation and "the main process exited" all end with `TerminateJobObject`, so the whole tree goes,
  including grandchildren and background processes the program left behind (`leftover_processes_killed`).
  Tests: `test_timeout_kills_the_whole_tree`, `test_leftover_background_child_is_killed_when_main_exits`,
  `test_spawn_kill_tree_for_previews`.
- **Memory caps.** Per-process committed memory (default 1 GiB for CLI runs) and whole-job memory (default 2 GiB).
  An allocation over the cap fails inside the program; the job reports it and the run records `process_memory` /
  `job_memory`. Test: `test_memory_cap_triggers`.
- **Process count cap** (default 32 for CLI runs). Test: `test_active_process_limit_triggers`.
- **Crash handling.** `DIE_ON_UNHANDLED_EXCEPTION`, so a crash does not hang on a Windows Error Reporting dialog.
- **UI restrictions** (CLI runs, `strict`): no clipboard read/write, no desktop switching/creation, no global atoms,
  no USER handles from outside the job, no system parameter/display setting changes, no logoff/shutdown.
  Native previews use `interactive` (clipboard and USER handles allowed, so the user can work with the app).
- **Low integrity level.** The program runs with a duplicate of your token at integrity Low (S-1-16-4096) via
  `CreateProcessAsUserW`. The mandatory "no write up" policy denies writes to anything at Medium, which is the default
  for your profile, Documents, Desktop, the original's install folder, other drives' user folders, and HKCU.
  The scenario folder and its isolated home get a Low (OI)(CI) label so they stay writable.
  Tests: `test_low_integrity_denies_write_to_user_profile_but_work_dir_is_writable`,
  `test_low_integrity_is_the_cause_medium_control_can_write`, `test_cargo_build_script_cannot_write_user_profile`.
- **Scrubbed environment.** Only an allowlist is passed: `SystemRoot`, `windir`, `SystemDrive`, `ComSpec`, `PATHEXT`,
  processor/OS info, `ProgramFiles*`/`CommonProgramFiles*`/`ProgramData`/`PUBLIC`; `PATH` = program dir (+ runtime dir) +
  `System32`, `Windows`, `Wbem`. `TEMP`, `TMP`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `HOMEDRIVE`, `HOMEPATH`
  point into a sibling folder `.<scenario>.isohome` (outside the compared files). Then the launch spec's `env` and the
  step's `env` are added. API keys, tokens and anything else in the controller's environment are not passed.
  Test: `test_env_does_not_leak_host_secret` (sets `REBUILD_TEST_SECRET` and `OPENAI_API_KEY`, asserts both absent).
- **Handle hygiene.** Only the three std pipe handles are inherited (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`).
- **Output caps.** stdout/stderr are capped (default 8 MiB each); past the cap the pipe is drained and discarded so the
  program never blocks; truncation is recorded. Test: `test_output_cap_truncates_and_records`.
- **Wall-time cap** per step (scenario `timeout`, default 60 s).

Not guaranteed in `low` mode:

- **Reading.** A low-integrity process can still READ most of your files (profile, documents, browser profiles that
  are not otherwise protected), because files do not carry "no read up". Anything it reads it could send out:
- **Network is NOT blocked.** No firewall rules are installed (that needs administrator rights, and Rebuild Studio does
  not change system firewall or security settings). The program can connect to the internet and to local services.
- Shared, world-writable or Low-labelled locations (for example `%USERPROFILE%\AppData\LocalLow`, some temp areas,
  folders whose ACLs grant Everyone write) remain writable.
- Kernel or driver exploits, privilege-escalation bugs, and sandbox-escape vulnerabilities in Windows are out of scope.
- GUI interaction: UIPI blocks sending messages to higher-integrity windows, but the program can still draw windows,
  play sound, and (in previews) use the clipboard.
- CPU usage is bounded only by wall time (no CPU rate cap).

Compatibility verified on this machine (Windows 11 Home 10.0.26200, standard user, Medium integrity controller):
the PE CLI fixture (`fixtures/pecli`), the .NET fixture (`fixtures/dotnetapp`, `dotnet` host), Python helper programs,
`cmd.exe`/`.cmd` files, `curl.exe`, and `cargo build` (rustup toolchain + MSVC linker, with a case-local `CARGO_HOME`)
all run correctly at Low integrity. If a program does not work at Low (typically: it insists on writing to its own
install folder, HKCU, or `%APPDATA%` resolved through the shell API rather than the environment), you can lower
isolation explicitly and the downgrade is recorded:

```json
"launch": {"type": "exe", "path": "app.exe", "isolation": {"integrity": "medium", "reason": "writes settings to HKCU"}}
```

`integrity: "medium"` is refused without a `reason`. A scenario can carry the same `isolation` object. If the host cannot
create a Low token at all, the run fails with an explanation unless `"allow_downgrade": true`, in which case it
falls back to Medium and records `downgrade: "automatic: ..."`. Other knobs: `memory_mb`, `job_memory_mb`,
`max_processes`, `stdout_cap_bytes`, `stderr_cap_bytes`.

### `medium` mode (explicit downgrade)

Job Object, caps, UI restrictions, scrubbed environment and handle hygiene as above, but the program runs with your
normal token: it can read AND write everything you can. Used by default only for the web harness (Chromium's own
renderer sandbox needs a normal-integrity browser process; the web page itself runs inside Chromium's sandbox).

### `appcontainer` mode (opt-in, blocks network without admin)

`"isolation": {"integrity": "appcontainer"}` or `{"network": "blocked"}`. The program runs in an AppContainer with
no capabilities. It has no network access (no `internetClient`/`privateNetwork`; loopback is also denied) and can only
open objects that grant `ALL APPLICATION PACKAGES` or the container SID: system folders (read/execute) and the work
folders (granted with `icacls`). It cannot read your profile or documents. Test:
`test_appcontainer_mode_blocks_network_low_mode_does_not` (curl to a local HTTP server: succeeds at Low, fails in the
AppContainer).

Costs and limits of this mode:

- The program folder is copied into the isolated home (`.program`, max 1 GiB) because the container cannot read your
  folders; the original folder's ACLs are never changed.
- Interpreted programs whose runtime lives in your profile (for example a per-user Python or Node install) cannot start,
  because the container cannot read that runtime. Self-contained executables and programs using only system DLLs work.
- On first use it creates a per-user AppContainer profile named `RebuildStudio.Isolation` (an HKCU mapping plus a
  per-user `Packages` folder; no administrator rights, no system setting). `_sandbox_win.remove_appcontainer_profile()`
  deletes it. The `/isolation` probe never creates it (it reports `available: "on_first_use"`).
- It is not used by default because many real programs need network or user-profile reads.

### Builds and previews on Windows

- `cargo build` runs in `low` mode: build scripts and proc-macros can write only the candidate's source/`target` folder
  and a case-local `CARGO_HOME` (`<case>/build-cache/cargo-home`), not your profile or `~/.cargo`. Network stays open
  (crates.io downloads). Caps: 8 GiB per process, 16 GiB per job, 512 processes, stage time limit. The build result
  records `build_isolation`. Long-lived helpers left behind (for example `mspdbsrv.exe`) are killed with the job.
- Native previews run in `low` mode with `interactive` UI restrictions, no wall time, 2 GiB per process / 4 GiB per job,
  64 processes; their only writable folder is the preview state folder. Stop kills the whole job.
- The web harness (node + Chromium) runs in `medium` mode as explained above.

## Linux / macOS (POSIX)

- New session and process group per run; timeout, cancel and main-process exit `killpg(SIGKILL)` the group.
  A child that calls `setsid()` itself can escape the group (no cgroup is used).
- `RLIMIT_DATA` (memory, from the same `memory` setting), `RLIMIT_CORE=0`, `RLIMIT_FSIZE` (4 GiB), optional `RLIMIT_CPU`.
  Memory exhaustion shows up as the program's own allocation failure; it is not separately reported in `triggered`.
- The same allow-listed environment (`PATH` = program dir + `/usr/local/bin:/usr/bin:/bin`, `HOME`/`TMPDIR`/`XDG_*`
  redirected) and output/wall-time caps.
- No filesystem confinement: the program runs as your user and can read and write everything you can.
  No network blocking. No process-count cap (`RLIMIT_NPROC` is per user and would affect your other processes).
- PE programs run via wine are labelled `runner: "wine (non-certifying host runner)"`; wine runs under the same limits.
  These POSIX paths are kept for Linux CI; they were not exercised on the Windows development host.

## The stronger option: a disposable VM

For a program you suspect is malicious, none of the above is enough (reads and network are open in the default mode,
and kernel exploits are out of scope). Use a throwaway virtual machine instead:

1. Create a VM (Hyper-V on Windows Pro/Enterprise, VirtualBox or VMware on any edition including Home; Windows
   Sandbox is not available on Home). Use a fresh Windows image with no personal accounts or files.
2. Disable or restrict the VM's network adapter if the program does not need it, and do not share folders read-write.
3. Install Rebuild Studio (or only the original program) in the VM, run the capture there, and export the baseline
   (`launch_profile.baseline_file` format, see `docs/API.md`).
4. On your machine, create the case with that `baseline_file`; nothing is executed locally and no consent is needed.
5. Revert the VM to its snapshot (or delete it) afterwards.
