# Scanner desktop monitor

Local Qt window for an explicitly selected workstation recovery
attempt. It observes the existing service; it does not own the worker process.

Use the system Python with **PySide6 6.6 or newer** installed (already available on the
development workstation). The backend virtualenv does not require Qt.

The panel uses the sage/green/rust/blue/teal Scanner palette with light surfaces
and dark supporting text for contrast. Qt still draws the native controls,
keyboard focus, menus, and scrolling; no replacement widget theme is installed.

The main view fits its 520×600 window at the workstation's normal system font,
including every action button. Run selection and related metrics share rows.
Opening the checklist or recent log grows the window to fit the extra content;
closing them returns it to the compact height. A scroll fallback preserves access
on smaller screens, manually shortened windows, or with larger system fonts.
The bounded log viewer retains its own scrolling for long logs.

```zsh
cd /path/to/scanner
/usr/bin/python -m desktop.scanner_panel \
  --run /path/to/run/recovery-r003 \
  --unit scanner-reconstruct-object-20260924-recovery-r003.service
```

- Close hides the window when a tray is available. Click the tray icon or run
  the same command again to reopen it. Without a tray, normal taskbar minimize
  works and closing quits the panel.
- **Panel → Quit panel** exits only the monitor. Reconstruction continues.
- **Choose run…** selects an attempt directory and its exact user service. The
  service must still be loaded and its command must identify that directory;
  mismatches are rejected. The service name is filled from `plan.json` when
  available; older plans require entering it. Switching does not control workers.
- **Copy diagnostic summary** copies status, stage, timing, memory, error, and
  evidence paths. Full raw logs and commands are omitted; common credential
  patterns in errors are redacted. Local paths remain: review before sharing.
- **Checklist** separates process success from OBJ/material/
  texture file presence and BLEND/GLB presence in the output folder. This is not
  pixel decoding, Blender import validation, or visual approval. Partial output
  is not checked until the selected attempt succeeds.
- **Show recent log** expands a bounded log tail; **Output folder** opens the
  directory recorded in the plan. Observing, hiding, and closing never execute
  reconstruction commands.
- Terminal-state notifications occur for transitions observed while the panel
  is open, including when hidden. Opening an old failed run does not notify.
- RAM is service-cgroup usage, including charged cache; GPU memory is whole-GPU
  usage, not attributed to the selected job.
- Percentages and tool-reported ETAs apply to the named current operation, not
  the whole scan. Old progress is withdrawn after 30 seconds without a log
  update. Quiet logs do not prove a stall. No overall ETA is invented.
- Elapsed time describes this attempt, not previous attempts. A stopped service
  overrides stale `running` evidence. A missing service query shows uncertainty.

The monitor supports recovery directories with `state.json`,
`plan.json`, and `logs/<current_stage>.log`. It does not automatically discover
arbitrary new attempts or integrate the app's separate backend Jobs store.
Panel-created retries are followed immediately and on reopen via the run's
`.panel-latest.json`; other attempts must be selected explicitly.
The application-menu launcher passes `--restore-selection` to remember the last
selection using local Qt settings. Explicit CLI launches without this flag retain
their supplied target when opening a new panel. There is one panel instance per
user session; launching it again raises the existing window. Notifications apply
only to the selected attempt, not every reconstruction on the workstation.

## Guarded manual resume

**Review texture resume…** becomes available for failed/interrupted texturing.
The background preflight must succeed before a confirmation can launch a worker.
Only the current native OpenMVS 2.4.0 four-thread OBJ profile is supported.
Earlier stages, changed settings/tool binaries, and unverifiable services require
engineer review. A retry uses the same settings and can encounter the same error;
it does not automatically solve OOM or quality problems.

Preflight verifies service ownership and inactivity, no other active Scanner
reconstruction service, a successful mesh stage, source calibration, regular-file
inputs, input SHA-256 hashes, the pinned binary, and at least 44 GiB available RAM.
Confirmation defaults to Cancel. Launch revalidates the confirmed fingerprints
under a workspace lock and creates an exclusive `panel-resume-<id>` directory.
The worker rechecks inputs, retains the 48 GiB/no-swap limit and sleep inhibition,
and runs only the fixed TextureMesh command—not an old launcher or arbitrary
commands read from JSON. It starts the failed texture stage from the beginning.

OBJ/material/texture outputs go to that attempt's `output/`, never over prior
outputs. OpenMVS may write its own timestamped diagnostic log in the source
workspace; scan/mesh/image inputs are fingerprinted again after execution.
Nonempty outputs and referenced texture files are checked, but successful process
completion is not proof of visual quality. Failed and partial attempts are retained.
Locks serialize panel retries and share the legacy recovery lock where available;
external commands launched outside this workflow are not controlled by the panel.

Resume tests use disposable inputs and a tiny stand-in executable; systemd launch
is mocked there. The real running reconstruction is never stopped to test retry.

No listener is exposed on the network. A user-local Qt socket only reopens an
already running panel for the same attempt. No login autostart is installed.

Verification:

```zsh
/usr/bin/python -m unittest discover -s tests -p 'test_desktop*.py' -v
```

The pure adapter tests need only Python. GUI tests use Qt's offscreen platform
and skip when PySide6 is absent; actual KDE tray/notification delivery still
depends on desktop availability and notification settings.
