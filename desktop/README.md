# Scanner desktop monitor

Local Qt window for an explicitly selected workstation recovery
attempt. It observes the existing service; it does not own the worker process.

Use the system Python with **PySide6 6.6 or newer** and **Pillow** installed
(already available on the development workstation). Pillow is also an existing
backend dependency and is used for texture decoding. The backend virtualenv does not require Qt.

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
  live service command must identify that directory; mismatches are rejected.
  Completed historical attempts remain selectable after a transient unit is
  collected, provided any recorded service matches. This read-only selection
  does not relax the separate execution/recovery checks. The service name is filled from `plan.json` when
  available; older plans require entering it. Switching does not control workers.
- **Copy diagnostic summary** copies status, stage, timing, memory, error, and
  evidence paths. Full raw logs and commands are omitted; common credential
  patterns in errors are redacted. Local paths remain: review before sharing.
- **Checklist** separates process success from OBJ/material/
  texture file presence and BLEND/GLB presence in the output folder. This is not
  visual approval. New texture runs add `texture_quality.json`, which records
  full image decoding and bounded samples at face-used UV coordinates; a
  mostly-black result is a warning requiring review, not proof a dark subject
  is invalid. Old runs without this report remain explicitly unverified.
  Partial output is not checked until the selected attempt succeeds.
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
Earlier stages, unsupported settings/tool binaries, and unverifiable services
require engineer review. Retry profile v2 explicitly uses full-resolution
textures with global and local seam leveling disabled: native OpenMVS 2.4 tests
on this workstation reproduced black textures with either seam option enabled.
Known v1 plans can be checked as input, but their unreviewed saved commands are
never executed. The confirmation explains the new policy. Retries can still
encounter resource or quality problems.

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
Texture images are decoded and sampled before the texture stage succeeds; corrupt
outputs fail and remain eligible for guarded retry. A near-black warning is
recorded separately from native success. Successful process completion is not
proof of visual quality. Failed and partial attempts are retained.
Workspace locks preserve recovery evidence, while the shared heavy-job guard
serializes updated API, CLI, texture, and Blender workflows for the same user.
Admission is claimed inside the worker and retained by its native child. The
guard does not govern arbitrary commands or other applications, and does not
replace memory limits. Existing unguarded workers must finish before migration.

Resume tests use disposable inputs and a tiny stand-in executable; systemd launch
is mocked there. The real running reconstruction is never stopped to test retry.

No listener is exposed on the network. A user-local Qt socket only reopens an
already running panel for the same attempt. No login autostart is installed.

## Blender follow-up

**Auto-create .blend** is opt-in for the selected run and requires the panel to
be open or in the tray. It waits for the reconstruction service to become inactive,
not merely for its state file to say success. **Create .blend** and guarded
**Retry .blend** use the same worker. Each attempt creates a separate
`blender-<id>/output/scan.blend` and preserves earlier results.

The worker pins inputs/code/tool hashes, applies the workstation memory guard,
screens source texture pixels, imports the mesh, embeds the images, and reopens
the saved file. Reopening checks UV availability and connected material textures
as well as packed image data. A warning remains visible in the checklist if
source textures are mostly near-black. **Open .blend** opens only a verified
file; you still need to inspect its appearance and geometry in Blender.

The memory ceiling is a job limit, not a quality setting. If the kernel stops the
job at that ceiling, save other work and free RAM before requesting a fresh
attempt. Do not automatically reduce texture resolution or repeatedly restart.

Verification:

```zsh
/usr/bin/python -m unittest discover -s tests -p 'test_desktop*.py' -v
```

The pure adapter tests need only Python. GUI tests use Qt's offscreen platform
and skip when PySide6 is absent; actual KDE tray/notification delivery still
depends on desktop availability and notification settings.
