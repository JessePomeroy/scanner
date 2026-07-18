# Post-Capture Mask Authoring Contract

Status: version 1.0 contract, backend validation, ordered keep/erase
rasterization, package persistence, five-sample selection, and representative-
frame iPhone draft editor implemented. The temporal generator and sampled
approval gate are the next slices.

`metadata/mask_authoring.json` records what the user means to retain before a
propagation implementation creates the full `masks/capture` set. Source JPEGs
are never modified. The contract uses normalized capture-image coordinates so
it remains independent of screen size, device orientation, and the eventual
on-device or workstation mask generator.

```json
{
  "schema_version": "1.0",
  "authoring_mode": "representative_frames",
  "coordinate_space": "normalized_capture_image",
  "mask_convention": "white_keep_black_exclude",
  "revision": 1,
  "representative_frames": [
    {
      "frame_id": 49,
      "image": "images/frame_000049.jpg",
      "regions": [
        {
          "operation": "keep",
          "points": [
            {"x": 0.15, "y": 0.20},
            {"x": 0.85, "y": 0.20},
            {"x": 0.85, "y": 0.90},
            {"x": 0.15, "y": 0.90}
          ]
        },
        {
          "operation": "erase",
          "points": [
            {"x": 0.45, "y": 0.40},
            {"x": 0.60, "y": 0.40},
            {"x": 0.55, "y": 0.60}
          ]
        }
      ]
    }
  ]
}
```

Regions are applied in array order. `keep` paints white and `erase` paints
black, so a later erase polygon can cut a hole from an earlier keep polygon.
Each representative frame must contain at least one keep region. Multiple keep
regions allow disconnected scene subjects while retaining any intentionally
selected ground between them.

The backend rejects unknown or missing fields, unsupported identifiers,
non-finite/out-of-range points, degenerate polygons, erase-only frames,
duplicate representative frames, unsafe or oversized files, and any
`frame_id`/`image` pair that does not exactly match `frames.json`. Limits are 16
representative frames, 64 regions per frame, and 4,096 points per region.

Sampled propagation review uses stable first, quartile, middle, three-quarter,
and last frame indices with duplicates removed for short captures. This is the
minimum review set, not a substitute for automatically flagging low-confidence
or abruptly changing masks.

In the iPhone Scans tab, the paintbrush button opens the editable scan folder
and shows those five photos. Draw green Keep areas or red Erase areas, navigate
between samples, then choose **Save Draft**. Saving increments the revision,
atomically replaces `metadata/mask_authoring.json`, and rebuilds the ZIP through
a temporary archive. If rebuilding fails, the prior authoring file is restored.
The UI labels this as a draft because reconstruction must not consume it until
full-frame propagation and review have succeeded.
