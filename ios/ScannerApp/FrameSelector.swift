import ARKit
import Foundation
import simd

/// Decides whether the current camera frame should be kept as a keyframe.
///
/// The selector rejects poor tracking, throttles accepted frames, and accepts
/// useful movement from either translation or camera rotation.
struct FrameSelector {
    var minimumTranslationMeters: Float = 0.05
    var minimumRotationDegrees: Float = 6
    var minimumTimeInterval: TimeInterval = 0.45

    private var lastAcceptedTransform: simd_float4x4?
    private var lastAcceptedTimestamp: TimeInterval?

    mutating func shouldKeepFrame(_ frame: ARFrame) -> Bool {
        let decision = decision(for: frame)
        if decision.accepted {
            recordAcceptedFrame(frame)
        }
        return decision.accepted
    }

    func decision(for frame: ARFrame) -> FrameSelectionDecision {
        guard frame.camera.trackingState.isNormal else {
            return FrameSelectionDecision(
                accepted: false,
                reason: .tracking,
                translationMeters: nil,
                rotationDegrees: nil,
                secondsSincePreviousFrame: nil,
                movementSpeedMetersPerSecond: nil
            )
        }

        guard let lastAcceptedTransform else {
            return FrameSelectionDecision(
                accepted: true,
                reason: .firstFrame,
                translationMeters: nil,
                rotationDegrees: nil,
                secondsSincePreviousFrame: nil,
                movementSpeedMetersPerSecond: nil
            )
        }

        let currentPosition = frame.camera.transform.translation
        let lastPosition = lastAcceptedTransform.translation
        let distanceMoved = simd_distance(currentPosition, lastPosition)

        let rotationChanged = rotationAngleDegrees(
            from: lastAcceptedTransform,
            to: frame.camera.transform
        )

        let secondsSincePreviousFrame = lastAcceptedTimestamp.map { frame.timestamp - $0 }
        let movementSpeed = secondsSincePreviousFrame.flatMap { seconds -> Float? in
            guard seconds > 0 else { return nil }
            return distanceMoved / Float(seconds)
        }

        if let lastAcceptedTimestamp,
           frame.timestamp - lastAcceptedTimestamp < minimumTimeInterval {
            return FrameSelectionDecision(
                accepted: false,
                reason: .tooSoon,
                translationMeters: distanceMoved,
                rotationDegrees: rotationChanged,
                secondsSincePreviousFrame: secondsSincePreviousFrame,
                movementSpeedMetersPerSecond: movementSpeed
            )
        }

        guard distanceMoved >= minimumTranslationMeters ||
              rotationChanged >= minimumRotationDegrees else {
            return FrameSelectionDecision(
                accepted: false,
                reason: .insufficientMotion,
                translationMeters: distanceMoved,
                rotationDegrees: rotationChanged,
                secondsSincePreviousFrame: secondsSincePreviousFrame,
                movementSpeedMetersPerSecond: movementSpeed
            )
        }

        return FrameSelectionDecision(
            accepted: true,
            reason: .usefulMotion,
            translationMeters: distanceMoved,
            rotationDegrees: rotationChanged,
            secondsSincePreviousFrame: secondsSincePreviousFrame,
            movementSpeedMetersPerSecond: movementSpeed
        )
    }

    mutating func recordAcceptedFrame(_ frame: ARFrame) {
        self.lastAcceptedTransform = frame.camera.transform
        lastAcceptedTimestamp = frame.timestamp
    }

    mutating func reset() {
        lastAcceptedTransform = nil
        lastAcceptedTimestamp = nil
    }

    private func rotationAngleDegrees(from first: simd_float4x4, to second: simd_float4x4) -> Float {
        let firstRotation = simd_quatf(first.rotationMatrix)
        let secondRotation = simd_quatf(second.rotationMatrix)
        let delta = secondRotation * firstRotation.inverse
        let radians = 2 * acos(min(1, abs(delta.real)))
        return radians * 180 / .pi
    }
}

struct FrameSelectionDecision: Equatable {
    let accepted: Bool
    let reason: FrameRejectionReason
    let translationMeters: Float?
    let rotationDegrees: Float?
    let secondsSincePreviousFrame: TimeInterval?
    let movementSpeedMetersPerSecond: Float?
}

enum FrameRejectionReason: String, Equatable {
    case firstFrame
    case usefulMotion
    case tracking
    case tooSoon
    case insufficientMotion
    case blurry
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

    var rotationMatrix: simd_float3x3 {
        simd_float3x3(
            SIMD3<Float>(columns.0.x, columns.0.y, columns.0.z),
            SIMD3<Float>(columns.1.x, columns.1.y, columns.1.z),
            SIMD3<Float>(columns.2.x, columns.2.y, columns.2.z)
        )
    }
}
