import ARKit
import AVFoundation
import Foundation

/// Coordinates a scan session from start to finish.
///
/// This type will eventually connect AR tracking, high-resolution camera capture,
/// metadata recording, and scan package export.
final class ScanCaptureManager {
    private let arTrackingManager: ARTrackingManager
    private let cameraCaptureManager: CameraCaptureManager
    private let packageWriter: ScanPackageWriter

    private var frameSelector = FrameSelector()
    private var capturedFrames: [CapturedFrameMetadata] = []
    private var currentScanDirectory: URL?
    private var currentScanId: String?
    private var frameCounter = 0

    init(
        arTrackingManager: ARTrackingManager = ARTrackingManager(),
        cameraCaptureManager: CameraCaptureManager = CameraCaptureManager(),
        packageWriter: ScanPackageWriter = ScanPackageWriter()
    ) {
        self.arTrackingManager = arTrackingManager
        self.cameraCaptureManager = cameraCaptureManager
        self.packageWriter = packageWriter
    }

    func startScan(scanId: String = ScanCaptureManager.makeScanId()) throws {
        capturedFrames.removeAll()
        frameSelector.reset()
        frameCounter = 0
        currentScanId = scanId
        currentScanDirectory = try packageWriter.createNewScanFolder(scanId: scanId)

        arTrackingManager.startTracking()
        try cameraCaptureManager.prepareCamera()
        cameraCaptureManager.startRunning()
    }

    @discardableResult
    func stopScan() throws -> URL {
        guard let currentScanDirectory,
              let currentScanId else {
            throw ScanPackageWriterError.invalidScanId
        }

        arTrackingManager.stopTracking()
        cameraCaptureManager.stopRunning()

        let session = ScanSessionMetadata(
            scanId: currentScanId,
            createdAt: ISO8601DateFormatter().string(from: Date()),
            device: "iPhone",
            appVersion: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0",
            scanMode: "environment_photogrammetry",
            usesLidar: ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth),
            usesARKitMesh: ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh),
            imageCount: capturedFrames.count,
            depthFrameCount: 0,
            notes: "Initial capture package."
        )

        try packageWriter.saveFrameMetadata(capturedFrames, in: currentScanDirectory)
        try packageWriter.saveSessionMetadata(session, in: currentScanDirectory)
        return try packageWriter.zipScanFolder(at: currentScanDirectory)
    }

    func considerCurrentFrameForCapture() {
        guard let frame = arTrackingManager.currentFrame,
              frameSelector.shouldKeepFrame(frame) else {
            return
        }

        frameCounter += 1
        let imagePath = "images/frame_\(String(format: "%06d", frameCounter)).jpg"

        capturedFrames.append(
            CapturedFrameMetadata(
                id: frameCounter,
                imagePath: imagePath,
                depthPath: nil,
                timestamp: frame.timestamp,
                cameraTransform: frame.camera.transform.rows,
                intrinsics: frame.camera.intrinsics.rows,
                resolution: [
                    Int(frame.camera.imageResolution.width),
                    Int(frame.camera.imageResolution.height)
                ],
                trackingState: frame.camera.trackingState.description,
                blurScore: 1.0,
                exposureDuration: nil,
                iso: nil,
                whiteBalanceLocked: false,
                focusLocked: false
            )
        )

        // High-resolution still capture will be triggered here and written to imagePath.
        // The photo delegate must preserve this frame id so metadata and image stay paired.
    }

    private static func makeScanId() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy_MM_dd_HH_mm_ss"
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return "scan_\(formatter.string(from: Date()))"
    }
}

private extension ARCamera.TrackingState {
    var description: String {
        switch self {
        case .normal:
            return "normal"
        case .notAvailable:
            return "not_available"
        case .limited(let reason):
            return "limited_\(reason)"
        @unknown default:
            return "unknown"
        }
    }
}

private extension simd_float4x4 {
    var rows: [[Float]] {
        [
            [columns.0.x, columns.1.x, columns.2.x, columns.3.x],
            [columns.0.y, columns.1.y, columns.2.y, columns.3.y],
            [columns.0.z, columns.1.z, columns.2.z, columns.3.z],
            [columns.0.w, columns.1.w, columns.2.w, columns.3.w]
        ]
    }
}

private extension simd_float3x3 {
    var rows: [[Float]] {
        [
            [columns.0.x, columns.1.x, columns.2.x],
            [columns.0.y, columns.1.y, columns.2.y],
            [columns.0.z, columns.1.z, columns.2.z]
        ]
    }
}
