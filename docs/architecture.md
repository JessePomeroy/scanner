# Architecture

The project is split into two foundations:

- `ios/ScannerApp`: captures images and AR metadata into a portable scan package.
- `backend/app`: validates uploaded scan packages and runs reconstruction tools.

The iOS processing-history view depends on a small job-loading interface. A
URLSession adapter reads the owned FastAPI status contract in production, while
an in-memory adapter and mock-HTTP verifier exercise the same interface without
a live backend.

The first end-to-end target is:

1. iPhone creates `scan_id.zip`.
2. Backend receives and validates the package.
3. COLMAP produces `dense/fused.ply`.
4. OpenMVS optionally produces a textured mesh.
5. Blender automation can convert or simplify final assets.
