# Architecture

The project is split into two foundations:

- `ios/ScannerApp`: captures images and AR metadata into a portable scan package.
- `backend/app`: validates uploaded scan packages and runs reconstruction tools.

Raw JSON field parsing is owned by `scan_metadata`, which returns typed frame,
session, and video records. `scan_validator` owns filesystem/reference
integrity and exposes the existing `validate_scan_package` interface to backend
and script callers. This keeps metadata-shape compatibility rules out of
reconstruction planners.

Incoming upload persistence depends on a minimal async binary-reader contract.
FastAPI's `UploadFile` is the production source, while focused fake readers
exercise chunking, failure, and cancellation. The storage module owns temporary
files, off-loop sink workers, file/directory sync, no-clobber publication, and
cleanup. The upload-lifecycle module owns best-effort terminal job recording,
and the API endpoint owns HTTP error mapping.

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
