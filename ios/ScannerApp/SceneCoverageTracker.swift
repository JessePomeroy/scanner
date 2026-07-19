import Foundation
import simd

struct SceneCoverageSnapshot: Equatable, Sendable {
    let acceptedPoseCount: Int
    let uniquePositionCellCount: Int
    let headingBinCount: Int
    let elevationBinCount: Int
    let pathLengthMeters: Float
    let disconnectedJumpCount: Int
    let score: Float
    let guidance: String

    static let empty = SceneCoverageSnapshot(
        acceptedPoseCount: 0,
        uniquePositionCellCount: 0,
        headingBinCount: 0,
        elevationBinCount: 0,
        pathLengthMeters: 0,
        disconnectedJumpCount: 0,
        score: 0,
        guidance: "Begin with a slow connected pass across the scene"
    )

    var percent: Int {
        Int((score * 100).rounded())
    }

    var shouldWarnBeforeFinish: Bool {
        acceptedPoseCount < 24
            || headingBinCount < 4
            || elevationBinCount < 2
            || score < 0.7
            || disconnectedJumpCount > 0
    }
}

struct SceneCoverageTracker {
    var positionCellSizeMeters: Float = 0.4
    var headingBinCount = 8
    var elevationThresholdDegrees: Float = 15

    private var acceptedPoseCount = 0
    private var positionCells: Set<PositionCell> = []
    private var headingBins: Set<Int> = []
    private var elevationBins: Set<Int> = []
    private var previousPosition: SIMD3<Float>?
    private var pathLengthMeters: Float = 0
    private var disconnectedJumpAnchors: [SIMD3<Float>] = []

    mutating func record(cameraTransform: simd_float4x4) -> SceneCoverageSnapshot {
        let position = SIMD3<Float>(
            cameraTransform.columns.3.x,
            cameraTransform.columns.3.y,
            cameraTransform.columns.3.z
        )
        let forward = -SIMD3<Float>(
            cameraTransform.columns.2.x,
            cameraTransform.columns.2.y,
            cameraTransform.columns.2.z
        )
        acceptedPoseCount += 1
        if let previousPosition {
            let stepDistance = simd_distance(previousPosition, position)
            if stepDistance > 1.25 {
                disconnectedJumpAnchors.append(previousPosition)
            } else {
                pathLengthMeters += stepDistance
            }
        }
        previousPosition = position
        disconnectedJumpAnchors.removeAll {
            simd_distance($0, position) <= positionCellSizeMeters * 1.5
        }
        positionCells.insert(PositionCell(position, cellSize: positionCellSizeMeters))

        let yaw = atan2(forward.x, forward.z)
        let normalizedYaw = (yaw + .pi) / (2 * .pi)
        let heading = min(
            headingBinCount - 1,
            max(0, Int(floor(normalizedYaw * Float(headingBinCount))))
        )
        headingBins.insert(heading)

        let horizontalLength = max(0.0001, hypot(forward.x, forward.z))
        let elevationDegrees = atan2(forward.y, horizontalLength) * 180 / .pi
        elevationBins.insert(
            elevationDegrees > elevationThresholdDegrees
                ? 1
                : (elevationDegrees < -elevationThresholdDegrees ? -1 : 0)
        )
        return snapshot
    }

    mutating func reset() {
        acceptedPoseCount = 0
        positionCells.removeAll()
        headingBins.removeAll()
        elevationBins.removeAll()
        previousPosition = nil
        pathLengthMeters = 0
        disconnectedJumpAnchors.removeAll()
    }

    var snapshot: SceneCoverageSnapshot {
        if acceptedPoseCount == 0 {
            return .empty
        }
        let frameProgress = min(1, Float(acceptedPoseCount) / 24)
        let pathProgress = min(1, pathLengthMeters / 2)
        let positionProgress = min(1, Float(positionCells.count) / 6)
        let headingProgress = min(1, Float(headingBins.count) / 5)
        let elevationProgress = min(1, Float(elevationBins.count) / 2)
        let baseScore = min(
            1,
            frameProgress * 0.2
                + pathProgress * 0.2
                + positionProgress * 0.2
                + headingProgress * 0.25
                + elevationProgress * 0.15
        )
        let disconnectedJumpCount = disconnectedJumpAnchors.count
        let score = max(0, baseScore - min(0.4, Float(disconnectedJumpCount) * 0.12))
        return SceneCoverageSnapshot(
            acceptedPoseCount: acceptedPoseCount,
            uniquePositionCellCount: positionCells.count,
            headingBinCount: headingBins.count,
            elevationBinCount: elevationBins.count,
            pathLengthMeters: pathLengthMeters,
            disconnectedJumpCount: disconnectedJumpCount,
            score: score,
            guidance: guidance(score: score)
        )
    }

    private func guidance(score: Float) -> String {
        if !disconnectedJumpAnchors.isEmpty {
            return "Bridge the path gap with overlapping frames before moving on"
        }
        if acceptedPoseCount < 8 {
            return "Build a slow connected pass with overlapping views"
        }
        if pathLengthMeters < 1 || positionCells.count < 4 {
            return "Move farther across the scene; avoid scanning from one spot"
        }
        if headingBins.count < 4 {
            return "Add side and diagonal views of the same structures"
        }
        if elevationBins.count < 2 {
            return "Add a higher or lower pass while keeping overlap"
        }
        if acceptedPoseCount < 24 || score < 0.75 {
            return "Bridge remaining gaps with slow overlapping frames"
        }
        return "Coverage looks connected; fill any visible gaps before stopping"
    }
}

private struct PositionCell: Hashable {
    let x: Int
    let y: Int
    let z: Int

    init(_ position: SIMD3<Float>, cellSize: Float) {
        let safeCellSize = max(0.05, cellSize)
        x = Int(floor(position.x / safeCellSize))
        y = Int(floor(position.y / safeCellSize))
        z = Int(floor(position.z / safeCellSize))
    }
}
