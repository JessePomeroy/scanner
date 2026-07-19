import ARKit
import AVFoundation
import Combine
import CoreMotion
import Foundation
import ImageIO
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
    @Published private(set) var sceneCoverage = SceneCoverageSnapshot.empty
    @Published private(set) var sceneCameraPath: [SIMD3<Float>] = []
    @Published private(set) var sceneSurfaceSamples: [SceneSurfaceSample] = []

    private let arTrackingManager: ARTrackingManager
    private let cameraCaptureManager: CameraCaptureManager
    private let packageWriter: ScanPackageWriter
    private let imageWriter: ARFrameImageWriter
    private let maskRasterizer = CaptureMaskRasterizer()
    private let maskCoordinateMapper = CaptureMaskCoordinateMapper()
    private let videoRecorder = ARFrameVideoRecorder()
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
    private var sceneCoverageTracker = SceneCoverageTracker()
    private var sceneCameraPositions: [SIMD3<Float>] = []
    private var scanStartedAt: Date?
    private var lastRejectedStatusTimestamp: TimeInterval = 0
    private var videoOutputURL: URL?
    private var videoRelativePath: String?
    private var videoCapturedAt: String?
    private var videoRecordingFailed = false
    private var videoDurationLimitMessageShown = false
    private var reconstructionPolygon: [NormalizedMaskPoint] = []
    private var reconstructionPreviewSize: CGSize?
    private var captureMaskCount = 0
    private var pendingKeyframeCapture: PendingKeyframeCapture?
    private var highResolutionImageCount = 0
    private var fallbackImageCount = 0

    private let minimumSharpnessScore: Float = 0.18
    private let fastMovementWarningMetersPerSecond: Float = 0.45
    private let maximumVideoDurationSeconds: Double = 30
    private let maximumHighResolutionWaitSeconds: TimeInterval = 2

    var arSession: ARSession {
        arTrackingManager.session
    }

    func startPreview() throws {
        try arTrackingManager.startTracking()
    }

    func stopPreview() {
        guard !isScanning else { return }
        arTrackingManager.stopTracking()
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
            sceneCoverageTracker.reset()
            sceneCameraPositions.removeAll(keepingCapacity: true)
            scanStartedAt = Date()
            lastRejectedStatusTimestamp = 0
            videoRelativePath = "video/scan.mov"
            videoOutputURL = scanDirectory.appendingPathComponent("video/scan.mov")
            videoCapturedAt = ISO8601DateFormatter().string(from: Date())
            videoRecordingFailed = false
            videoDurationLimitMessageShown = false
            captureMaskCount = 0
            pendingKeyframeCapture = nil
            highResolutionImageCount = 0
            fallbackImageCount = 0
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
            clearQualityMetrics: true,
            sceneCoverage: .empty,
            sceneCameraPath: [],
            sceneSurfaceSamples: []
        )

        do {
            motionRecorder.start()
            try arTrackingManager.startTracking()
        } catch {
            _ = motionRecorder.stop()
            captureQueue.sync {
                isScanning = false
                videoRecorder.cancel()
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
            try finishPendingKeyframeWithFallback()
            isScanning = false

            guard let currentScanDirectory,
                  let currentScanId else {
                throw ScanPackageWriterError.invalidScanId
            }

            let motionSamples = motionRecorder.stop()
            let videoMetadata = videoRecorder.finish().map { [$0] } ?? []
            let createdAt = ISO8601DateFormatter().string(from: Date())
            let appVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0"
            let buildVersion = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0"
            let usesLidar = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
            let usesARKitMesh = ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
            let finalSceneCoverage = scanMode == .scene
                ? sceneCoverageTracker.snapshot
                : nil
            let session = ScanSessionMetadata(
                scanId: currentScanId,
                createdAt: createdAt,
                device: UIDevice.current.model,
                appVersion: appVersion,
                buildVersion: buildVersion,
                scanMode: scanMode.rawValue,
                usesLidar: usesLidar,
                usesARKitMesh: usesARKitMesh,
                highResolutionFrameCaptureEnabled: arTrackingManager
                    .highResolutionFrameCaptureEnabled,
                configuredVideoResolution: arTrackingManager.configuredVideoResolution,
                highResolutionImageCount: highResolutionImageCount,
                fallbackImageCount: fallbackImageCount,
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
                sceneCoverage: finalSceneCoverage.map {
                    SceneCoverageMetadata(
                        schemaVersion: "1.1",
                        acceptedPoseCount: $0.acceptedPoseCount,
                        uniquePositionCellCount: $0.uniquePositionCellCount,
                        headingBinCount: $0.headingBinCount,
                        elevationBinCount: $0.elevationBinCount,
                        pathLengthMeters: $0.pathLengthMeters,
                        disconnectedJumpCount: $0.disconnectedJumpCount,
                        surfaceHitCount: $0.surfaceHitCount,
                        uniqueSurfaceCellCount: $0.uniqueSurfaceCellCount,
                        multiAngleSurfaceCellCount: $0.multiAngleSurfaceCellCount,
                        minimumSurfaceDistanceMeters: $0.minimumSurfaceDistanceMeters,
                        maximumSurfaceDistanceMeters: $0.maximumSurfaceDistanceMeters,
                        surfaceScore: $0.surfaceScore,
                        score: $0.score
                    )
                },
                notes: scanMode == .object
                    ? "Object scan package with ARKit subject center metadata."
                    : "Scene scan package."
            )

            var manifest = ScanPackageManifest(
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
                    "pose-synchronized ARKit high-resolution keyframes are preferred but individual captures may fall back to the triggering video frame",
                    "video capture is encoded from ARFrame camera buffers, not separate high-resolution video",
                    "video capture is capped at 30 seconds to keep scan exports manageable",
                    "scene surface coverage uses ARKit estimated raycasts and is capture guidance, not reconstruction proof",
                    "automatic object crop requires ARKit-to-COLMAP coordinate alignment",
                    "dense reconstruction requires a CUDA-capable COLMAP build"
                ]
            )
            if !reconstructionPolygon.isEmpty {
                guard captureMaskCount == capturedFrames.count,
                      captureMaskCount > 0 else {
                    throw ScanCaptureMaskError.incompleteMaskSet(
                        expected: capturedFrames.count,
                        actual: captureMaskCount
                    )
                }
                manifest.reconstructionScope = ReconstructionScopeManifest(
                    schemaVersion: "1.0",
                    mode: "image_masks",
                    maskSpace: "capture_image",
                    maskConvention: "white_keep_black_exclude",
                    maskCount: captureMaskCount
                )
            }

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
                highResolutionImageCount: highResolutionImageCount,
                fallbackImageCount: fallbackImageCount,
                videoCount: videoMetadata.count,
                averageBlurScore: qualityStats.averageBlurScore,
                minimumBlurScore: qualityStats.minimumBlurScore,
                maximumMovementSpeedMetersPerSecond: qualityStats.maximumMovementSpeed,
                captureDurationSeconds: session.captureDurationSeconds,
                objectRadiusMeters: session.objectRadiusMeters,
                objectCenterWasSet: session.objectCenterWorld != nil,
                sceneCoverageScore: finalSceneCoverage?.score
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
        captureQueue.sync {
            cleanupCaptureAfterFailure()
        }
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

    func configureReconstructionArea(
        _ polygon: [NormalizedMaskPoint],
        previewSize: CGSize
    ) {
        do {
            try maskRasterizer.validate(polygon)
            guard previewSize.width > 0, previewSize.height > 0 else { return }
            captureQueue.sync {
                reconstructionPolygon = polygon
                reconstructionPreviewSize = previewSize
            }
        } catch {
            return
        }
    }

    func clearReconstructionArea() {
        captureQueue.sync {
            reconstructionPolygon.removeAll()
            reconstructionPreviewSize = nil
        }
    }

    private func considerFrameForCapture(_ frame: ARFrame) {
        guard isScanning,
              currentScanDirectory != nil else {
            return
        }
        if let pending = pendingKeyframeCapture {
            guard frame.timestamp - pending.fallbackFrame.timestamp
                    >= maximumHighResolutionWaitSeconds else {
                return
            }
            pendingKeyframeCapture = nil
            frameSelector.recordAcceptedFrame(pending.fallbackFrame)
            do {
                try packageAcceptedFrame(
                    pending.fallbackFrame,
                    decision: pending.decision,
                    blurScore: pending.fallbackBlurScore,
                    imageSource: .arkitVideoFrameFallback,
                    highResolutionFailure: "high_resolution_capture_timeout"
                )
            } catch {
                failFramePackaging(error)
                return
            }
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

        let token = UUID()
        pendingKeyframeCapture = PendingKeyframeCapture(
            token: token,
            fallbackFrame: frame,
            decision: decision,
            fallbackBlurScore: blurScore
        )
        cameraCaptureManager.captureKeyframe(
            from: arSession,
            fallbackFrame: frame,
            completionQueue: captureQueue
        ) { [weak self] captured in
            self?.completePendingKeyframe(token: token, captured: captured)
        }
    }

    private func completePendingKeyframe(
        token: UUID,
        captured: CapturedKeyframe
    ) {
        guard isScanning,
              let pending = pendingKeyframeCapture,
              pending.token == token else {
            return
        }
        pendingKeyframeCapture = nil

        let capturedBlurScore = imageWriter.estimateSharpnessScore(
            from: captured.frame.capturedImage
        )
        let capturedDecision = frameSelector.decision(for: captured.frame)
        if captured.source == .arkitHighResolution,
           capturedBlurScore >= minimumSharpnessScore,
           capturedDecision.accepted {
            frameSelector.recordAcceptedFrame(captured.frame)
            do {
                try packageAcceptedFrame(
                    captured.frame,
                    decision: capturedDecision,
                    blurScore: capturedBlurScore,
                    imageSource: captured.source,
                    highResolutionFailure: nil
                )
            } catch {
                failFramePackaging(error)
            }
        } else {
            frameSelector.recordAcceptedFrame(pending.fallbackFrame)
            do {
                try packageAcceptedFrame(
                    pending.fallbackFrame,
                    decision: pending.decision,
                    blurScore: pending.fallbackBlurScore,
                    imageSource: .arkitVideoFrameFallback,
                    highResolutionFailure: captured.highResolutionFailure
                        ?? "high_resolution_frame_blurry_or_pose_rejected"
                )
            } catch {
                failFramePackaging(error)
            }
        }
    }

    private func finishPendingKeyframeWithFallback() throws {
        guard let pending = pendingKeyframeCapture else { return }
        pendingKeyframeCapture = nil
        frameSelector.recordAcceptedFrame(pending.fallbackFrame)
        try packageAcceptedFrame(
            pending.fallbackFrame,
            decision: pending.decision,
            blurScore: pending.fallbackBlurScore,
            imageSource: .arkitVideoFrameFallback,
            highResolutionFailure: "scan_stopped_before_high_resolution_completion"
        )
    }

    private func packageAcceptedFrame(
        _ frame: ARFrame,
        decision: FrameSelectionDecision,
        blurScore: Float,
        imageSource: KeyframeImageSource,
        highResolutionFailure: String?
    ) throws {
        guard let currentScanDirectory else { return }
        frameCounter += 1
        let imagePath = "images/frame_\(String(format: "%06d", frameCounter)).jpg"
        let imageURL = currentScanDirectory.appendingPathComponent(imagePath)

        let savedImage = try imageWriter.writeJPEG(from: frame.capturedImage, to: imageURL)
        if !reconstructionPolygon.isEmpty {
            guard let reconstructionPreviewSize else {
                throw ScanCaptureMaskError.missingPreviewSize
            }
            let imagePolygon = try maskCoordinateMapper.imagePolygon(
                from: reconstructionPolygon,
                previewSize: reconstructionPreviewSize,
                imageWidth: savedImage.width,
                imageHeight: savedImage.height
            )
            let pngData = try maskRasterizer.pngData(
                for: imagePolygon,
                width: savedImage.width,
                height: savedImage.height
            )
            try packageWriter.saveCaptureMask(
                pngData,
                forImagePath: imagePath,
                in: currentScanDirectory
            )
            captureMaskCount += 1
        }

        let exposure = exposureMetadata(for: frame)
        capturedFrames.append(
            CapturedFrameMetadata(
                id: frameCounter,
                imagePath: imagePath,
                imageSource: imageSource.rawValue,
                highResolutionCaptureFailure: highResolutionFailure,
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
                exposureDuration: exposure.duration,
                iso: exposure.iso,
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

        switch imageSource {
        case .arkitHighResolution:
            highResolutionImageCount += 1
        case .arkitVideoFrameFallback:
            fallbackImageCount += 1
        }
        qualityStats.recordAccepted(blurScore: blurScore, movementSpeed: decision.movementSpeedMetersPerSecond)
        let surfacePoint = scanMode == .scene
            ? estimatedSceneSurfacePoint(for: frame)
            : nil
        let coverage = scanMode == .scene
            ? sceneCoverageTracker.record(
                cameraTransform: frame.camera.transform,
                surfacePoint: surfacePoint
            )
            : nil
        var cameraPath: [SIMD3<Float>]?
        if scanMode == .scene {
            sceneCameraPositions.append(frame.camera.transform.translation)
            if sceneCameraPositions.count > 300 {
                sceneCameraPositions.removeFirst(sceneCameraPositions.count - 300)
            }
            cameraPath = sceneCameraPositions
        }
        let count = capturedFrames.count
        let imageQualityLabel = imageSource == .arkitHighResolution
            ? "high-res"
            : "fallback"
        updatePublishedState(
            statusMessage: "Accepted \(count) \(imageQualityLabel)",
            guidanceMessage: guidance(
                for: decision,
                blurScore: blurScore,
                sceneCoverage: coverage
            ),
            acceptedFrameCount: count,
            lastBlurScore: blurScore,
            lastMovementSpeed: decision.movementSpeedMetersPerSecond,
            sceneCoverage: coverage,
            sceneCameraPath: cameraPath,
            sceneSurfaceSamples: scanMode == .scene
                ? sceneCoverageTracker.surfaceSamples
                : nil
        )
    }

    private func failFramePackaging(_ error: Error) {
        cleanupCaptureAfterFailure()
        arTrackingManager.stopTracking()
        updatePublishedState(
            state: .failed("Frame packaging failed"),
            statusMessage: "Frame packaging failed: \(error.localizedDescription)"
        )
    }

    private func exposureMetadata(for frame: ARFrame) -> (duration: Double?, iso: Float?) {
        let metadata = frame.exifData
        let nested = metadata[kCGImagePropertyExifDictionary as String] as? [String: Any]
        let exposure = (
            metadata[kCGImagePropertyExifExposureTime as String]
                ?? nested?[kCGImagePropertyExifExposureTime as String]
        ) as? NSNumber
        let isoValue = metadata[kCGImagePropertyExifISOSpeedRatings as String]
            ?? nested?[kCGImagePropertyExifISOSpeedRatings as String]
        let iso: Float?
        if let values = isoValue as? [NSNumber] {
            iso = values.first?.floatValue
        } else {
            iso = (isoValue as? NSNumber)?.floatValue
        }
        return (exposure?.doubleValue, iso)
    }

    private func estimatedSceneSurfacePoint(for frame: ARFrame) -> SIMD3<Float>? {
        let transform = frame.camera.transform
        let origin = transform.translation
        let direction = simd_normalize(
            -SIMD3<Float>(
                transform.columns.2.x,
                transform.columns.2.y,
                transform.columns.2.z
            )
        )
        let query = ARRaycastQuery(
            origin: origin,
            direction: direction,
            allowing: .estimatedPlane,
            alignment: .any
        )
        return arSession.raycast(query).first?.worldTransform.translation
    }

    private func recordVideoFrame(_ frame: ARFrame) {
        guard isScanning,
              !videoRecordingFailed,
              let videoOutputURL,
              let videoRelativePath,
              let videoCapturedAt else {
            return
        }

        do {
            if !videoRecorder.isRecording {
                try videoRecorder.start(
                    outputURL: videoOutputURL,
                    relativePath: videoRelativePath,
                    capturedAt: videoCapturedAt,
                    firstFrame: frame
                )
            }

            videoRecorder.append(frame, maximumDurationSeconds: maximumVideoDurationSeconds)

            if videoRecorder.reachedDurationLimit,
               !videoDurationLimitMessageShown {
                videoDurationLimitMessageShown = true
                updatePublishedState(
                    statusMessage: "Video capped",
                    guidanceMessage: "Continuing keyframe capture; video is capped for export size"
                )
            }
        } catch {
            videoRecordingFailed = true
            videoRecorder.cancel()
            updatePublishedState(
                statusMessage: "Video unavailable",
                guidanceMessage: "Continuing with keyframe image capture"
            )
        }
    }

    private func cleanupCaptureAfterFailure() {
        isScanning = false
        pendingKeyframeCapture = nil
        _ = motionRecorder.stop()
        videoRecorder.cancel()
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
        clearQualityMetrics: Bool = false,
        sceneCoverage: SceneCoverageSnapshot? = nil,
        sceneCameraPath: [SIMD3<Float>]? = nil,
        sceneSurfaceSamples: [SceneSurfaceSample]? = nil
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
            if let sceneCoverage {
                self.sceneCoverage = sceneCoverage
            }
            if let sceneCameraPath {
                self.sceneCameraPath = sceneCameraPath
            }
            if let sceneSurfaceSamples {
                self.sceneSurfaceSamples = sceneSurfaceSamples
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

    private func guidance(
        for decision: FrameSelectionDecision,
        blurScore: Float,
        sceneCoverage: SceneCoverageSnapshot?
    ) -> String {
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

        return sceneCoverage?.guidance
            ?? "Capture overlapping angles, not just straight-on passes"
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

private struct PendingKeyframeCapture {
    let token: UUID
    let fallbackFrame: ARFrame
    let decision: FrameSelectionDecision
    let fallbackBlurScore: Float
}

private enum ScanCaptureMaskError: LocalizedError {
    case missingPreviewSize
    case incompleteMaskSet(expected: Int, actual: Int)

    var errorDescription: String? {
        switch self {
        case .missingPreviewSize:
            return "The reconstruction area preview size is missing."
        case let .incompleteMaskSet(expected, actual):
            return "The scan has \(actual) masks for \(expected) captured images."
        }
    }
}

extension ScanCaptureManager: ARSessionDelegate {
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        recordVideoFrame(frame)
        considerFrameForCapture(frame)
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        cleanupCaptureAfterFailure()
        updatePublishedState(state: .failed(error.localizedDescription), statusMessage: error.localizedDescription)
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
