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

The backend artifact module owns the boundary between internal reconstruction
paths and downloadable results. It rebases declared outputs when a processing
workspace moves to completed storage, discovers only existing regular files,
and validates both persisted and requested paths against the configured
completed/failed roots. The FastAPI layer maps those outcomes to a typed
artifact manifest and streams an already-opened descriptor without duplicating
containment logic. POSIX no-follow directory descriptors close the validation-
to-open race, and multi-link files are excluded because path containment alone
cannot prove ownership of a hard-linked inode.

The iOS processing-history view depends on a small job-loading interface. A
URLSession adapter reads the owned FastAPI status contract in production, while
an in-memory adapter and mock-HTTP verifier exercise the same interface without
a live backend.

Result retrieval has a separate `ReconstructionArtifactAccessing` boundary. Its
URLSession adapter decodes the typed manifest and downloads into a system file
rather than materializing an entire reconstruction result as `Data`. The client
validates identifiers, relative paths, filenames, duplicate declarations, media
types, and the final regular-file byte count before moving the file into an
app-owned temporary directory. That ownership boundary also removes files after
sharing, cancellation, navigation away, and the next launch after an interrupted
process. The artifact store owns loading and one-at-a-time download state; the
SwiftUI view owns navigation and share-sheet presentation.

The iOS gallery upload path similarly depends on a `ScanUploading` interface.
The HTTP adapter reuses the job model and shared backend URL policy, builds a
multipart body from the ZIP in bounded chunks on a background task, and uploads
the temporary body file through a small transport seam. The upload store owns
one-at-a-time UI state and notices; the gallery owns user actions and prevents
deleting the ZIP while it is being prepared or uploaded.

The first end-to-end target is:

1. iPhone creates `scan_id.zip`.
2. Backend receives and validates the package.
3. COLMAP produces `dense/fused.ply`.
4. OpenMVS optionally produces a textured mesh.
5. Blender automation can convert or simplify final assets.
