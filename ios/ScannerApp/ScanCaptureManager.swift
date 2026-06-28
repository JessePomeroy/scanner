import ARKit
import AVFoundation
import Combine
import Foundation
import UIKit

enum ScanCaptureState: Equatable {
    case idle
    case scanning
    case exporting
    case completed(URL)
    case failed(String)
}

/// Coordinates a scan session from start to finish.
///
/// This type will eventually connect AR tracking, high-resolution camera capture,
/// metadata recording, and scan package export.
final class ScanCaptureManager: NSObject, ObservableObject {
    @Published private(set) var state: ScanCaptureState = .idle
    @Published private(set) var acceptedFrameCount = 0
    @Published private(set) var statusMessage = "Ready"
    @Published private(set) var lastZipURL: URL?

    private let arTrackingManager: ARTrackingManager
    private let cameraCaptureManager: CameraCaptureManager
    private let packageWriter: ScanPackageWriter
    private let imageWriter: ARFrameImageWriter
    private let captureQueue = DispatchQueue(label: "ScannerApp.ScanCaptureManager.capture")

    private var frameSelector = FrameSelector()
    private var capturedFrames: [CapturedFrameMetadata] = []
    private var currentScanDirectory: URL?
    private var currentScanId: String?
    private var frameCounter = 0
    private var isScanning = false

    var arSession: ARSession {
        arTrackingManager.session
    }

    init(
        arTrackingManager: ARTrackingManager = ARTrackingManager(),
        cameraCaptureManager: CameraCaptureManager = CameraCaptureManager(),
        packageWriter: ScanPackageWriter = ScanPackageWriter(),
        imageWriter: ARFrameImageWriter = ARFrameImageWriter()
    ) {
        self.arTrackingManager = arTrackingManager
        self.cameraCaptureManager = cameraCaptureManager
        self.packageWriter = packageWriter
        self.imageWriter = imageWriter
        super.init()
        self.arTrackingManager.setDelegate(self, queue: captureQueue)
    }

    func startScan(scanId: String = ScanCaptureManager.makeScanId()) throws {
        let scanDirectory = try packageWriter.createNewScanFolder(scanId: scanId)

        captureQueue.sync {
            capturedFrames.removeAll()
            frameSelector.reset()
            frameCounter = 0
            currentScanId = scanId
            currentScanDirectory = scanDirectory
            isScanning = true
        }

        updatePublishedState(
            state: .scanning,
            statusMessage: "Scanning",
            acceptedFrameCount: 0,
            clearZipURL: true
        )

        do {
            try arTrackingManager.startTracking()
        } catch {
            captureQueue.sync {
                isScanning = false
            }
            updatePublishedState(state: .failed(error.localizedDescription), statusMessage: error.localizedDescription)
            throw error
        }
    }

    @discardableResult
    func stopScan() throws -> URL {
        updatePublishedState(state: .exporting, statusMessage: "Exporting")
        arTrackingManager.stopTracking()

        let zipURL: URL = try captureQueue.sync {
            isScanning = false

            guard let currentScanDirectory,
                  let currentScanId else {
                throw ScanPackageWriterError.invalidScanId
            }

            let session = ScanSessionMetadata(
                scanId: currentScanId,
                createdAt: ISO8601DateFormatter().string(from: Date()),
                device: UIDevice.current.model,
                appVersion: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0",
                scanMode: "environment_photogrammetry",
                usesLidar: ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth),
                usesARKitMesh: ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh),
                imageCount: capturedFrames.count,
                depthFrameCount: 0,
                notes: "ARFrame image capture package."
            )

            try packageWriter.saveFrameMetadata(capturedFrames, in: currentScanDirectory)
            try packageWriter.saveSessionMetadata(session, in: currentScanDirectory)
            return try packageWriter.zipScanFolder(at: currentScanDirectory)
        }

        updatePublishedState(
            state: .completed(zipURL),
            statusMessage: "Exported \(zipURL.lastPathComponent)",
            lastZipURL: zipURL
        )

        return zipURL
    }

    func fail(_ error: Error) {
        isScanning = false
        arTrackingManager.stopTracking()
        updatePublishedState(state: .failed(error.localizedDescription), statusMessage: error.localizedDescription)
    }

    private func considerFrameForCapture(_ frame: ARFrame) {
        guard isScanning,
              frameSelector.shouldKeepFrame(frame),
              let currentScanDirectory else {
            return
        }

        frameCounter += 1
        let imagePath = "images/frame_\(String(format: "%06d", frameCounter)).jpg"
        let imageURL = currentScanDirectory.appendingPathComponent(imagePath)

        let savedImage: SavedFrameImage
        do {
            savedImage = try imageWriter.writeJPEG(from: frame.capturedImage, to: imageURL)
        } catch {
            updatePublishedState(
                state: .failed("Image write failed"),
                statusMessage: "Image write failed: \(error.localizedDescription)"
            )
            isScanning = false
            return
        }

        capturedFrames.append(
            CapturedFrameMetadata(
                id: frameCounter,
                imagePath: imagePath,
                depthPath: nil,
                timestamp: frame.timestamp,
                cameraTransform: frame.camera.transform.rows,
                intrinsics: frame.camera.intrinsics.rotatedRight(
                    sourceHeight: Float(frame.camera.imageResolution.height)
                ).rows,
                resolution: [
                    savedImage.width,
                    savedImage.height
                ],
                trackingState: frame.camera.trackingState.description,
                blurScore: 1.0,
                exposureDuration: nil,
                iso: nil,
                whiteBalanceLocked: false,
                focusLocked: false
            )
        )

        let count = capturedFrames.count
        updatePublishedState(statusMessage: "Accepted \(count) frames", acceptedFrameCount: count)
    }

    private func updatePublishedState(
        state: ScanCaptureState? = nil,
        statusMessage: String? = nil,
        acceptedFrameCount: Int? = nil,
        lastZipURL: URL? = nil,
        clearZipURL: Bool = false
    ) {
        DispatchQueue.main.async {
            if let state {
                self.state = state
            }
            if let statusMessage {
                self.statusMessage = statusMessage
            }
            if let acceptedFrameCount {
                self.acceptedFrameCount = acceptedFrameCount
            }
            if clearZipURL {
                self.lastZipURL = nil
            }
            if let lastZipURL {
                self.lastZipURL = lastZipURL
            }
        }
    }

    private static func makeScanId() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy_MM_dd_HH_mm_ss"
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return "scan_\(formatter.string(from: Date()))"
    }
}

extension ScanCaptureManager: ARSessionDelegate {
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        considerFrameForCapture(frame)
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        updatePublishedState(state: .failed(error.localizedDescription), statusMessage: error.localizedDescription)
        isScanning = false
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

    func rotatedRight(sourceHeight: Float) -> simd_float3x3 {
        let fx = columns.0.x
        let fy = columns.1.y
        let cx = columns.2.x
        let cy = columns.2.y

        return simd_float3x3(
            SIMD3<Float>(fy, 0, 0),
            SIMD3<Float>(0, fx, 0),
            SIMD3<Float>(sourceHeight - cy, cx, 1)
        )
    }
}
