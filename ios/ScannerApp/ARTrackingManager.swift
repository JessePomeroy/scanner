import ARKit
import Foundation

enum ARTrackingManagerError: LocalizedError {
    case worldTrackingUnavailable

    var errorDescription: String? {
        switch self {
        case .worldTrackingUnavailable:
            return "AR world tracking is not available on this device."
        }
    }
}

/// Owns ARKit session configuration and live tracking state.
///
/// This file will start ARWorldTrackingConfiguration, enable optional LiDAR scene
/// reconstruction/depth features, and expose the current ARFrame to capture code.
final class ARTrackingManager {
    let session: ARSession
    private(set) var highResolutionFrameCaptureEnabled = false
    private(set) var configuredVideoResolution: [Int]?

    var currentFrame: ARFrame? {
        session.currentFrame
    }

    init(session: ARSession = ARSession()) {
        self.session = session
    }

    func setDelegate(_ delegate: ARSessionDelegate?, queue: DispatchQueue?) {
        session.delegate = delegate
        session.delegateQueue = queue
    }

    func startTracking(resetSession: Bool = true) throws {
        guard ARWorldTrackingConfiguration.isSupported else {
            throw ARTrackingManagerError.worldTrackingUnavailable
        }

        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity

        if let highResolutionFormat = ARWorldTrackingConfiguration
            .recommendedVideoFormatForHighResolutionFrameCapturing {
            configuration.videoFormat = highResolutionFormat
            highResolutionFrameCaptureEnabled = true
        } else {
            highResolutionFrameCaptureEnabled = false
        }
        configuredVideoResolution = [
            Int(configuration.videoFormat.imageResolution.width),
            Int(configuration.videoFormat.imageResolution.height)
        ]

        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) {
            configuration.sceneReconstruction = .mesh
        }

        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            configuration.frameSemantics.insert(.sceneDepth)
        }

        let options: ARSession.RunOptions = resetSession
            ? [.resetTracking, .removeExistingAnchors]
            : []

        session.run(configuration, options: options)
    }

    func stopTracking() {
        session.pause()
    }
}
