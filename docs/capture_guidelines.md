# Capture Guidelines

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
