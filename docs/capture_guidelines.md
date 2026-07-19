# Capture Guidelines

## High-Resolution Keyframes

- On supported devices, the app configures ARKit's recommended high-resolution
  still format and requests one out-of-band, pose-synchronized high-resolution
  `ARFrame` for each accepted keyframe. Normal tracking and the support video
  continue while that request completes.
- Only one still request is active at a time. Keep moving slowly; useful-view
  selection resumes as soon as the prior keyframe is saved. A request that has
  not completed within two seconds falls back automatically so capture cannot
  stall.
- If a high-resolution request is unsupported, fails, is blurry, or is still
  pending when Stop is tapped, the already-qualified triggering video frame is
  packaged instead. The export summary shows **High-Res** and **Fallback**
  counts, and each frame records its image source and fallback reason.
- `metadata/session.json` records the configured AR video resolution and total
  high-resolution/fallback counts. The backend warns when a supported session
  falls back unusually often so the capture can be compared against texture
  quality.

- Move slowly and avoid fast pans.
- Keep 70-80% overlap between accepted frames.
- Capture surfaces from multiple angles and heights.
- Avoid reflective, transparent, glossy, blank, or moving surfaces.
- Prefer stable exposure, focus, and white balance.
- Use validation-only backend uploads while iterating on the iOS package format.
- Watch the live blur and speed readouts. Low blur scores or high movement
  speeds mean the scan may align, but texture quality will suffer.
- Treat rejected-frame count as a capture-health signal. Some rejected frames are
  normal; a very high rejected count usually means fast motion, weak tracking, or
  too little new camera angle.

## Object Scans

- Select `Object` mode before scanning.
- Start the scan, wait for stable tracking, then tap the subject in the camera view.
- Use `0.75m` for small tabletop items, `1.5m` for medium objects, and `3m` for larger outdoor objects.
- Circle the object from multiple angles and heights while keeping it in frame.
- Background texture can help camera alignment, but later processing should prefer the tapped object region.
- If you forget to tap the object, the scan can still reconstruct, but the object
crop planner will not know the intended subject center.

## Scene Scans

- Build one connected route through the scene. Do not capture isolated detail
  islands without overlapping frames that bridge back to the main pass.
- Watch the live Coverage percentage and follow its movement prompts. It
  combines accepted viewpoints, connected travel, horizontal view directions,
  and high/low passes.
- The cyan trail marks accepted camera positions. Keep it connected through
  deliberate turns; a large empty jump means the intervening scene may lack
  bridging frames. For very long captures, the overlay keeps the latest 300
  accepted positions so the live view stays responsive.
- Aim the dashed Coverage brush at the scene area you are currently working
  around. An ARKit surface hit appears amber; revisit it from a meaningfully
  different side or diagonal angle to turn that surface cell green. The surface
  paint is estimated capture evidence, not proof that photogrammetry will retain
  the surface.
- The percentage is a capture-motion heuristic, not proof that every surface is
  visible. Before stopping, still look for hidden backsides, door recesses,
  undersides, thin edges, and gaps behind foreground objects.
- If the app asks for side/diagonal views, keep recognizable structure from the
  previous view in frame while changing angle. If it asks for a higher/lower
  pass, preserve overlap rather than jumping to a disconnected viewpoint.
- If a large jump creates a path-gap warning, keep recording and walk a slow,
  overlapping route back to the earlier cyan trail. Reaching that route clears
  the unresolved gap. When Stop is tapped with weak coverage or an unresolved
  gap, choose **Continue Scanning** to repair it or **Finish Anyway** when the
  limitation is intentional.
- Coverage evidence is saved under `scene_coverage` in `metadata/session.json`
  and copied into the backend scan report. Scores below 55% add a
  `low_scene_coverage` warning; unresolved jumps add a
  `disconnected_scene_passes` warning for later comparison with reconstruction
  quality.
- Surface hit count, cell count, multi-angle cell count, distance range, and
  surface score are saved with that evidence. The backend flags weak multi-angle
  surface coverage and distance ranges wider than four meters for benchmark
  comparison; these are guidance signals rather than automatic scan rejection.
