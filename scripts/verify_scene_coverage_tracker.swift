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

        _ = tracker.record(cameraTransform: transform(position: .zero, yaw: 0))
        _ = tracker.record(
            cameraTransform: transform(position: SIMD3<Float>(3, 0, 0), yaw: 0)
        )
        let disconnected = tracker.snapshot
        try require(
            disconnected.disconnectedJumpCount == 1,
            "Expected a large accepted-pose jump to be flagged"
        )
        try require(
            disconnected.pathLengthMeters == 0,
            "Expected disconnected jumps not to reward connected path length"
        )
        try require(
            disconnected.guidance.contains("Bridge the path gap"),
            "Expected actionable path-gap guidance"
        )
        try require(disconnected.shouldWarnBeforeFinish, "Expected finish warning")

        for x in stride(from: Float(2.5), through: Float(0.5), by: -0.5) {
            _ = tracker.record(
                cameraTransform: transform(position: SIMD3<Float>(x, 0, 0), yaw: .pi)
            )
        }
        let reconnected = tracker.snapshot
        try require(
            reconnected.disconnectedJumpCount == 0,
            "Expected an overlapping return pass to resolve the path gap"
        )
        try require(
            reconnected.pathLengthMeters >= 2,
            "Expected the overlapping return pass to add connected path evidence"
        )

        tracker.reset()
        for index in 0..<8 {
            _ = tracker.record(
                cameraTransform: transform(
                    position: SIMD3<Float>(Float(index) * 0.02, 0, 1),
                    yaw: .pi
                ),
                surfacePoint: .zero
            )
        }
        let narrowSurface = tracker.snapshot
        try require(
            narrowSurface.guidance.contains("Brush across more scene surfaces"),
            "Expected guidance when the brush remains on one surface cell"
        )
        try require(narrowSurface.shouldWarnBeforeFinish, "Expected weak surface warning")

        tracker.reset()
        for x in stride(from: Float(0), through: Float(1.2), by: 0.4) {
            _ = tracker.record(
                cameraTransform: transform(position: SIMD3<Float>(x, 0, 1), yaw: .pi),
                surfacePoint: SIMD3<Float>(x, 0, 0)
            )
        }
        for x in stride(from: Float(1.2), through: Float(0), by: -0.4) {
            _ = tracker.record(
                cameraTransform: transform(position: SIMD3<Float>(x, 0, -1), yaw: 0),
                surfacePoint: SIMD3<Float>(x, 0, 0)
            )
        }
        let surface = tracker.snapshot
        try require(surface.surfaceHitCount == 8, "Expected every valid surface hit")
        try require(surface.uniqueSurfaceCellCount == 4, "Expected four surface cells")
        try require(
            surface.multiAngleSurfaceCellCount == 4,
            "Expected opposite views to cover every surface cell from multiple angles"
        )
        try require(surface.surfaceScore > 0.6, "Expected strong surface coverage")
        try require(
            tracker.surfaceSamples.allSatisfy(\.isWellCovered),
            "Expected green multi-angle surface samples"
        )
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
