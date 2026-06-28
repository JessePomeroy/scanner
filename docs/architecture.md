# Architecture

The project is split into two foundations:

- `ios/ScannerApp`: captures images and AR metadata into a portable scan package.
- `backend/app`: validates uploaded scan packages and runs reconstruction tools.

The first end-to-end target is:

1. iPhone creates `scan_id.zip`.
2. Backend receives and validates the package.
3. COLMAP produces `dense/fused.ply`.
4. OpenMVS optionally produces a textured mesh.
5. Blender automation can convert or simplify final assets.
