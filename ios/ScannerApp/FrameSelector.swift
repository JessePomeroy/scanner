import ARKit
import Foundation
import simd

/// Decides whether the current camera frame should be kept as a keyframe.
///
/// The first implementation will reject poor tracking and accept frames when the
/// camera has moved enough from the last accepted pose.
struct FrameSelector {
    var minimumTranslationMeters: Float = 0.15

    private var lastAcceptedTransform: simd_float4x4?

    mutating func shouldKeepFrame(_ frame: ARFrame) -> Bool {
        guard frame.camera.trackingState.isNormal else {
            return false
        }

        guard let lastAcceptedTransform else {
            self.lastAcceptedTransform = frame.camera.transform
            return true
        }

        let currentPosition = frame.camera.transform.translation
        let lastPosition = lastAcceptedTransform.translation
        let distanceMoved = simd_distance(currentPosition, lastPosition)

        guard distanceMoved > minimumTranslationMeters else {
            return false
        }

        self.lastAcceptedTransform = frame.camera.transform
        return true
    }

    mutating func reset() {
        lastAcceptedTransform = nil
    }
}

private extension ARCamera.TrackingState {
    var isNormal: Bool {
        if case .normal = self {
            return true
        }

        return false
    }
}

extension simd_float4x4 {
    var translation: SIMD3<Float> {
        SIMD3<Float>(columns.3.x, columns.3.y, columns.3.z)
    }
}
