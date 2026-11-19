#!/usr/bin/env bash
# Compile and run every portable Swift check for the iPhone code.
# Needs a Mac with Xcode's command-line tools; GitHub Actions runs it on each
# pull request that touches ios/. Every check runs even if an earlier one fails.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

app=ios/ScannerApp
out="${TMPDIR:-/tmp}/scanner-swift-verifiers"
mkdir -p "$out"
failed=()

check() {
  local name="$1"
  shift
  echo "== $name"
  if xcrun swiftc "$@" "scripts/$name.swift" -o "$out/$name" && "$out/$name"; then
    return 0
  fi
  failed+=("$name")
}

check verify_scan_zip_writer \
  $app/ScanMetadataModels.swift $app/MetadataWriter.swift $app/ScanPackageWriter.swift
check verify_capture_mask_mapper \
  $app/CaptureMaskRasterizer.swift $app/CaptureMaskCoordinateMapper.swift
check verify_mask_authoring_contract -warnings-as-errors -parse-as-library \
  $app/CaptureMaskRasterizer.swift
check verify_reconstruction_job_client \
  $app/ReconstructionJobClient.swift $app/ReconstructionJobStore.swift
check verify_scan_upload_client -warnings-as-errors \
  $app/ReconstructionJobClient.swift $app/ScanUploadClient.swift $app/ScanUploadStore.swift
check verify_reconstruction_artifact_client -warnings-as-errors \
  $app/ReconstructionJobClient.swift $app/ReconstructionArtifactClient.swift \
  $app/ReconstructionArtifactStore.swift
check verify_ply_point_cloud_loader -warnings-as-errors \
  $app/PLYPointCloudLoader.swift
check verify_reconstruction_scope_client -warnings-as-errors -parse-as-library \
  $app/ReconstructionJobClient.swift $app/ReconstructionScopeClient.swift
check verify_mask_review_client -warnings-as-errors -parse-as-library \
  $app/ReconstructionJobClient.swift $app/MaskReviewClient.swift
check verify_scene_coverage_tracker -warnings-as-errors -parse-as-library \
  $app/SceneCoverageTracker.swift

if ((${#failed[@]})); then
  echo "Failed: ${failed[*]}" >&2
  exit 1
fi
echo "All Swift checks passed."
