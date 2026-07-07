import ARKit
import AVFoundation
import Combine
import CoreMotion
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
    @Published private(set) var rejectedFrameCount = 0
    @Published private(set) var lastBlurScore: Float?
    @Published private(set) var lastMovementSpeed: Float?
    @Published private(set) var statusMessage = "Ready"
    @Published private(set) var guidanceMessage = "Choose a mode"
    @Published private(set) var lastZipURL: URL?
    @Published private(set) var lastExportSummary: ScanExportSummary?
    @Published var scanMode: ScanMode = .scene {
        didSet {
            objectCenterWorld = nil
            statusMessage = scanMode == .object ? "Tap subject while scanning" : "Ready"
        }
    }
    @Published var objectRadiusPreset: ObjectRadiusPreset = .medium
    @Published private(set) var objectCenterIsSet = false

    private let arTrackingManager: ARTrackingManager
    private let cameraCaptureManager: CameraCaptureManager
    private let packageWriter: ScanPackageWriter
    private let imageWriter: ARFrameImageWriter
    private let motionRecorder = MotionCaptureRecorder()
    private let captureQueue = DispatchQueue(label: "ScannerApp.ScanCaptureManager.capture")

    private var frameSelector = FrameSelector()
    private var capturedFrames: [CapturedFrameMetadata] = []
    private var currentScanDirectory: URL?
    private var currentScanId: String?
    private var frameCounter = 0
    private var isScanning = false
    private var objectCenterWorld: SIMD3<Float>?
    private var qualityStats = CaptureQualityStats()
    private var scanStartedAt: Date?
    private var lastRejectedStatusTimestamp: TimeInterval = 0

    private let minimumSharpnessScore: Float = 0.18
    private let fastMovementWarningMetersPerSecond: Float = 0.45

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
            objectCenterWorld = nil
            qualityStats = CaptureQualityStats()
            scanStartedAt = Date()
            lastRejectedStatusTimestamp = 0
            isScanning = true
        }

        updatePublishedState(
            state: .scanning,
            statusMessage: scanMode == .object ? "Tap subject" : "Scanning",
            guidanceMessage: scanMode == .object ? "Tap the subject, then move around it slowly" : "Move slowly and cover the scene from several angles",
            acceptedFrameCount: 0,
            rejectedFrameCount: 0,
            lastBlurScore: nil,
            lastMovementSpeed: nil,
            objectCenterIsSet: false,
            clearZipURL: true,
            clearExportSummary: true,
            clearQualityMetrics: true
        )

        do {
            motionRecorder.start()
            try arTrackingManager.startTracking()
        } catch {
            _ = motionRecorder.stop()
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

        let exportResult: (zipURL: URL, summary: ScanExportSummary) = try captureQueue.sync {
            isScanning = false

            guard let currentScanDirectory,
                  let currentScanId else {
                throw ScanPackageWriterError.invalidScanId
            }

            let motionSamples = motionRecorder.stop()
            let createdAt = ISO8601DateFormatter().string(from: Date())
            let appVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0"
            let buildVersion = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0"
            let usesLidar = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
            let usesARKitMesh = ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
            let videoMetadata: [VideoCaptureMetadata] = []
            let session = ScanSessionMetadata(
                scanId: currentScanId,
                createdAt: createdAt,
                device: UIDevice.current.model,
                appVersion: appVersion,
                buildVersion: buildVersion,
                scanMode: scanMode.rawValue,
                usesLidar: usesLidar,
                usesARKitMesh: usesARKitMesh,
                imageCount: capturedFrames.count,
                depthFrameCount: 0,
                imuSampleCount: motionSamples.count,
                videoCount: videoMetadata.count,
                rejectedFrameCount: qualityStats.rejectedTotal,
                rejectedTrackingCount: qualityStats.rejectedTracking,
                rejectedBlurCount: qualityStats.rejectedBlur,
                rejectedMotionCount: qualityStats.rejectedMotion,
                averageBlurScore: qualityStats.averageBlurScore,
                minimumBlurScore: qualityStats.minimumBlurScore,
                maximumMovementSpeedMetersPerSecond: qualityStats.maximumMovementSpeed,
                captureDurationSeconds: scanStartedAt.map { Date().timeIntervalSince($0) },
                objectCenterWorld: objectCenterWorld?.array,
                objectRadiusMeters: scanMode == .object ? objectRadiusPreset.rawValue : nil,
                notes: scanMode == .object
                    ? "Object scan package with ARKit subject center metadata."
                    : "Scene scan package."
            )

            let manifest = ScanPackageManifest(
                schemaVersion: "0.3.0",
                scanId: currentScanId,
                scanMode: scanMode.rawValue,
                appVersion: appVersion,
                buildVersion: buildVersion,
                imageCount: capturedFrames.count,
                depthFrameCount: 0,
                imuSampleCount: motionSamples.count,
                videoCount: videoMetadata.count,
                usesLidar: usesLidar,
                usesARKitMesh: usesARKitMesh,
                usesVideo: !videoMetadata.isEmpty,
                createdAt: createdAt,
                limitations: [
                    "depth frames are optional and absent on non-LiDAR devices",
                    "video capture metadata is scaffolded but recording is not implemented yet",
                    "automatic object crop requires ARKit-to-COLMAP coordinate alignment",
                    "dense reconstruction requires a CUDA-capable COLMAP build"
                ]
            )

            try packageWriter.saveFrameMetadata(capturedFrames, in: currentScanDirectory)
            try packageWriter.saveSessionMetadata(session, in: currentScanDirectory)
            try packageWriter.saveMotionMetadata(motionSamples, in: currentScanDirectory)
            try packageWriter.saveVideoMetadata(videoMetadata, in: currentScanDirectory)
            try packageWriter.saveManifest(manifest, in: currentScanDirectory)
            let zipURL = try packageWriter.zipScanFolder(at: currentScanDirectory)
            let summary = ScanExportSummary(
                scanId: currentScanId,
                zipFileName: zipURL.lastPathComponent,
                scanModeTitle: scanMode.title,
                acceptedFrameCount: capturedFrames.count,
                rejectedFrameCount: qualityStats.rejectedTotal,
                averageBlurScore: qualityStats.averageBlurScore,
                minimumBlurScore: qualityStats.minimumBlurScore,
                maximumMovementSpeedMetersPerSecond: qualityStats.maximumMovementSpeed,
                captureDurationSeconds: session.captureDurationSeconds,
                objectRadiusMeters: session.objectRadiusMeters,
                objectCenterWasSet: session.objectCenterWorld != nil
            )

            return (zipURL, summary)
        }

        updatePublishedState(
            state: .completed(exportResult.zipURL),
            statusMessage: "Exported \(exportResult.zipURL.lastPathComponent)",
            guidanceMessage: exportSummary(exportResult.summary),
            lastZipURL: exportResult.zipURL,
            lastExportSummary: exportResult.summary
        )

        return exportResult.zipURL
    }

    func fail(_ error: Error) {
        isScanning = false
        _ = motionRecorder.stop()
        arTrackingManager.stopTracking()
        updatePublishedState(state: .failed(error.localizedDescription), statusMessage: error.localizedDescription)
    }

    func setObjectCenter(_ worldPosition: SIMD3<Float>) {
        guard scanMode == .object else { return }

        captureQueue.sync {
            objectCenterWorld = worldPosition
        }

        updatePublishedState(
            statusMessage: "Subject set",
            guidanceMessage: "Circle the subject and keep it filling most of the frame",
            objectCenterIsSet: true
        )
    }

    private func considerFrameForCapture(_ frame: ARFrame) {
        guard isScanning,
              let currentScanDirectory else {
            return
        }

        let decision = frameSelector.decision(for: frame)
        guard decision.accepted else {
            recordRejectedFrame(reason: decision.reason, frame: frame, decision: decision)
            return
        }

        let blurScore = imageWriter.estimateSharpnessScore(from: frame.capturedImage)
        guard blurScore >= minimumSharpnessScore else {
            recordRejectedFrame(reason: .blurry, frame: frame, decision: decision, blurScore: blurScore)
            return
        }

        frameSelector.recordAcceptedFrame(frame)
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
                blurScore: blurScore,
                exposureDuration: nil,
                iso: nil,
                exposureTargetOffset: nil,
                ambientIntensity: frame.lightEstimate.map { Float($0.ambientIntensity) },
                ambientColorTemperature: frame.lightEstimate.map { Float($0.ambientColorTemperature) },
                whiteBalanceLocked: false,
                focusLocked: false,
                movementDeltaMeters: decision.translationMeters,
                rotationDeltaDegrees: decision.rotationDegrees,
                secondsSincePreviousFrame: decision.secondsSincePreviousFrame,
                movementSpeedMetersPerSecond: decision.movementSpeedMetersPerSecond
            )
        )

        qualityStats.recordAccepted(blurScore: blurScore, movementSpeed: decision.movementSpeedMetersPerSecond)
        let count = capturedFrames.count
        updatePublishedState(
            statusMessage: "Accepted \(count) frames",
            guidanceMessage: guidance(for: decision, blurScore: blurScore),
            acceptedFrameCount: count,
            lastBlurScore: blurScore,
            lastMovementSpeed: decision.movementSpeedMetersPerSecond
        )
    }

    private func recordRejectedFrame(
        reason: FrameRejectionReason,
        frame: ARFrame,
        decision: FrameSelectionDecision,
        blurScore: Float? = nil
    ) {
        qualityStats.recordRejected(reason: reason)

        guard frame.timestamp - lastRejectedStatusTimestamp > 0.75 else {
            return
        }

        lastRejectedStatusTimestamp = frame.timestamp
        updatePublishedState(
            statusMessage: rejectionStatus(reason: reason),
            guidanceMessage: rejectionGuidance(reason: reason),
            rejectedFrameCount: qualityStats.rejectedTotal,
            lastBlurScore: blurScore,
            lastMovementSpeed: decision.movementSpeedMetersPerSecond
        )
    }

    private func updatePublishedState(
        state: ScanCaptureState? = nil,
        statusMessage: String? = nil,
        guidanceMessage: String? = nil,
        acceptedFrameCount: Int? = nil,
        rejectedFrameCount: Int? = nil,
        lastBlurScore: Float? = nil,
        lastMovementSpeed: Float? = nil,
        objectCenterIsSet: Bool? = nil,
        lastZipURL: URL? = nil,
        lastExportSummary: ScanExportSummary? = nil,
        clearZipURL: Bool = false,
        clearExportSummary: Bool = false,
        clearQualityMetrics: Bool = false
    ) {
        DispatchQueue.main.async {
            if let state {
                self.state = state
            }
            if let statusMessage {
                self.statusMessage = statusMessage
            }
            if let guidanceMessage {
                self.guidanceMessage = guidanceMessage
            }
            if let acceptedFrameCount {
                self.acceptedFrameCount = acceptedFrameCount
            }
            if let rejectedFrameCount {
                self.rejectedFrameCount = rejectedFrameCount
            }
            if clearQualityMetrics {
                self.lastBlurScore = nil
                self.lastMovementSpeed = nil
            }
            if let lastBlurScore {
                self.lastBlurScore = lastBlurScore
            }
            if let lastMovementSpeed {
                self.lastMovementSpeed = lastMovementSpeed
            }
            if let objectCenterIsSet {
                self.objectCenterIsSet = objectCenterIsSet
            }
            if clearZipURL {
                self.lastZipURL = nil
            }
            if let lastZipURL {
                self.lastZipURL = lastZipURL
            }
            if clearExportSummary {
                self.lastExportSummary = nil
            }
            if let lastExportSummary {
                self.lastExportSummary = lastExportSummary
            }
        }
    }

    private static func makeScanId() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy_MM_dd_HH_mm_ss"
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return "scan_\(formatter.string(from: Date()))"
    }

    private func guidance(for decision: FrameSelectionDecision, blurScore: Float) -> String {
        if scanMode == .object && !objectCenterIsSet {
            return "Tap the subject so the object radius can be saved"
        }

        if let speed = decision.movementSpeedMetersPerSecond,
           speed > fastMovementWarningMetersPerSecond {
            return "Move slower for sharper overlap"
        }

        if blurScore < minimumSharpnessScore + 0.12 {
            return "Hold steadier; sharp frames reconstruct better"
        }

        if scanMode == .object {
            return "Keep circling and vary your height"
        }

        return "Capture overlapping angles, not just straight-on passes"
    }

    private func rejectionStatus(reason: FrameRejectionReason) -> String {
        switch reason {
        case .tracking:
            return "Tracking limited"
        case .tooSoon, .insufficientMotion:
            return "Looking for new angle"
        case .blurry:
            return "Frame too blurry"
        case .firstFrame, .usefulMotion:
            return "Frame skipped"
        }
    }

    private func rejectionGuidance(reason: FrameRejectionReason) -> String {
        switch reason {
        case .tracking:
            return "Aim at textured surfaces until tracking recovers"
        case .tooSoon:
            return "Keep moving slowly; overlap is good"
        case .insufficientMotion:
            return scanMode == .object ? "Move around the subject" : "Shift position or angle for more coverage"
        case .blurry:
            return "Slow down and avoid fast pans"
        case .firstFrame, .usefulMotion:
            return "Continue scanning"
        }
    }

    private func exportSummary(_ summary: ScanExportSummary) -> String {
        let blur = summary.averageBlurScore.map { String(format: "%.2f", $0) } ?? "n/a"
        return "Frames \(summary.acceptedFrameCount), rejected \(summary.rejectedFrameCount), avg blur \(blur)"
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

private struct CaptureQualityStats {
    private(set) var rejectedTotal = 0
    private(set) var rejectedTracking = 0
    private(set) var rejectedBlur = 0
    private(set) var rejectedMotion = 0
    private(set) var acceptedBlurScores: [Float] = []
    private(set) var maximumMovementSpeed: Float?

    var averageBlurScore: Float? {
        guard !acceptedBlurScores.isEmpty else { return nil }
        return acceptedBlurScores.reduce(0, +) / Float(acceptedBlurScores.count)
    }

    var minimumBlurScore: Float? {
        acceptedBlurScores.min()
    }

    mutating func recordAccepted(blurScore: Float, movementSpeed: Float?) {
        acceptedBlurScores.append(blurScore)

        if let movementSpeed {
            maximumMovementSpeed = max(maximumMovementSpeed ?? movementSpeed, movementSpeed)
        }
    }

    mutating func recordRejected(reason: FrameRejectionReason) {
        rejectedTotal += 1

        switch reason {
        case .tracking:
            rejectedTracking += 1
        case .blurry:
            rejectedBlur += 1
        case .tooSoon, .insufficientMotion:
            rejectedMotion += 1
        case .firstFrame, .usefulMotion:
            break
        }
    }
}

private final class MotionCaptureRecorder {
    private let manager = CMMotionManager()
    private let queue = OperationQueue()
    private let lock = NSLock()
    private var samples: [MotionSampleMetadata] = []

    func start() {
        lock.lock()
        samples.removeAll()
        lock.unlock()

        guard manager.isDeviceMotionAvailable else { return }

        queue.name = "ScannerApp.MotionCaptureRecorder"
        manager.deviceMotionUpdateInterval = 1.0 / 30.0
        manager.startDeviceMotionUpdates(to: queue) { [weak self] motion, _ in
            guard let self,
                  let motion else {
                return
            }

            let sample = MotionSampleMetadata(
                timestamp: motion.timestamp,
                attitudeQuaternion: [
                    motion.attitude.quaternion.x,
                    motion.attitude.quaternion.y,
                    motion.attitude.quaternion.z,
                    motion.attitude.quaternion.w
                ],
                rotationRate: [
                    motion.rotationRate.x,
                    motion.rotationRate.y,
                    motion.rotationRate.z
                ],
                gravity: [
                    motion.gravity.x,
                    motion.gravity.y,
                    motion.gravity.z
                ],
                userAcceleration: [
                    motion.userAcceleration.x,
                    motion.userAcceleration.y,
                    motion.userAcceleration.z
                ]
            )

            self.lock.lock()
            self.samples.append(sample)
            self.lock.unlock()
        }
    }

    func stop() -> [MotionSampleMetadata] {
        if manager.isDeviceMotionActive {
            manager.stopDeviceMotionUpdates()
        }

        lock.lock()
        let result = samples
        lock.unlock()
        return result
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

private extension SIMD3<Float> {
    var array: [Float] {
        [x, y, z]
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
