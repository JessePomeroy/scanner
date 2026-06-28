import ARKit
import Foundation

/// Owns ARKit session configuration and live tracking state.
///
/// This file will start ARWorldTrackingConfiguration, enable optional LiDAR scene
/// reconstruction/depth features, and expose the current ARFrame to capture code.
final class ARTrackingManager {
    let session: ARSession

    var currentFrame: ARFrame? {
        session.currentFrame
    }

    init(session: ARSession = ARSession()) {
        self.session = session
    }

    func startTracking(resetSession: Bool = true) {
        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity

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
