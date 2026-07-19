import Foundation
import simd

private enum CoverageVerificationError: Error {
    case failed(String)
}

@main
private enum SceneCoverageVerifier {
    static func main() throws {
        var tracker = SceneCoverageTracker()
        try require(tracker.snapshot == .empty, "Expected empty initial coverage")

        for index in 0..<8 {
            let position = SIMD3<Float>(Float(index) * 0.42, 0, 0)
            let yaw = Float(index % 5) * (.pi / 3)
            _ = tracker.record(cameraTransform: transform(position: position, yaw: yaw))
        }
        let horizontal = tracker.snapshot
        try require(horizontal.acceptedPoseCount == 8, "Expected every accepted pose")
        try require(horizontal.uniquePositionCellCount >= 6, "Expected position diversity")
        try require(horizontal.headingBinCount >= 4, "Expected heading diversity")
        try require(horizontal.pathLengthMeters > 2.5, "Expected connected path length")
        try require(
            horizontal.guidance.contains("higher or lower"),
            "Expected elevation guidance after a varied horizontal pass"
        )

        _ = tracker.record(
            cameraTransform: transform(
                position: SIMD3<Float>(3.4, 0.5, 0),
                yaw: .pi / 2,
                pitch: .pi / 6
            )
        )
        let elevated = tracker.snapshot
        try require(elevated.elevationBinCount >= 2, "Expected level and elevated bins")
        try require(elevated.score > horizontal.score, "Expected coverage score to improve")
        try require((0...100).contains(elevated.percent), "Expected bounded percentage")

        tracker.reset()
        try require(tracker.snapshot == .empty, "Expected reset to clear all evidence")
        print("Verified scene coverage tracker")
    }

    private static func transform(
        position: SIMD3<Float>,
        yaw: Float,
        pitch: Float = 0
    ) -> simd_float4x4 {
        let rotation = simd_quatf(angle: yaw, axis: SIMD3<Float>(0, 1, 0))
            * simd_quatf(angle: pitch, axis: SIMD3<Float>(1, 0, 0))
        var result = simd_float4x4(rotation)
        result.columns.3 = SIMD4<Float>(position, 1)
        return result
    }

    private static func require(_ condition: Bool, _ message: String) throws {
        if !condition {
            throw CoverageVerificationError.failed(message)
        }
    }
}
