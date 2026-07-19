import Foundation
import simd

struct SceneCoverageSnapshot: Equatable, Sendable {
    let acceptedPoseCount: Int
    let uniquePositionCellCount: Int
    let headingBinCount: Int
    let elevationBinCount: Int
    let pathLengthMeters: Float
    let disconnectedJumpCount: Int
    let surfaceHitCount: Int
    let uniqueSurfaceCellCount: Int
    let multiAngleSurfaceCellCount: Int
    let minimumSurfaceDistanceMeters: Float?
    let maximumSurfaceDistanceMeters: Float?
    let surfaceScore: Float
    let score: Float
    let guidance: String

    static let empty = SceneCoverageSnapshot(
        acceptedPoseCount: 0,
        uniquePositionCellCount: 0,
        headingBinCount: 0,
        elevationBinCount: 0,
        pathLengthMeters: 0,
        disconnectedJumpCount: 0,
        surfaceHitCount: 0,
        uniqueSurfaceCellCount: 0,
        multiAngleSurfaceCellCount: 0,
        minimumSurfaceDistanceMeters: nil,
        maximumSurfaceDistanceMeters: nil,
        surfaceScore: 0,
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
            || (surfaceHitCount >= 8 && surfaceScore < 0.45)
    }
}

struct SceneSurfaceSample: Equatable, Sendable {
    let id: String
    let position: SIMD3<Float>
    let observationCount: Int
    let viewAngleBinCount: Int

    var isWellCovered: Bool {
        observationCount >= 2 && viewAngleBinCount >= 2
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
    private var surfaceCells: [PositionCell: SurfaceCellEvidence] = [:]
    private var surfaceHitCount = 0
    private var minimumSurfaceDistanceMeters: Float?
    private var maximumSurfaceDistanceMeters: Float?
    private var observationIndex = 0

    mutating func record(
        cameraTransform: simd_float4x4,
        surfacePoint: SIMD3<Float>? = nil
    ) -> SceneCoverageSnapshot {
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
        if let surfacePoint {
            recordSurfacePoint(surfacePoint, cameraPosition: position)
        }
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
        surfaceCells.removeAll()
        surfaceHitCount = 0
        minimumSurfaceDistanceMeters = nil
        maximumSurfaceDistanceMeters = nil
        observationIndex = 0
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
        let multiAngleSurfaceCellCount = surfaceCells.values.filter {
            $0.viewDirections.count >= 2
        }.count
        let surfaceScore = sceneSurfaceScore(
            multiAngleSurfaceCellCount: multiAngleSurfaceCellCount
        )
        let score = max(0, baseScore - min(0.4, Float(disconnectedJumpCount) * 0.12))
        return SceneCoverageSnapshot(
            acceptedPoseCount: acceptedPoseCount,
            uniquePositionCellCount: positionCells.count,
            headingBinCount: headingBins.count,
            elevationBinCount: elevationBins.count,
            pathLengthMeters: pathLengthMeters,
            disconnectedJumpCount: disconnectedJumpCount,
            surfaceHitCount: surfaceHitCount,
            uniqueSurfaceCellCount: surfaceCells.count,
            multiAngleSurfaceCellCount: multiAngleSurfaceCellCount,
            minimumSurfaceDistanceMeters: minimumSurfaceDistanceMeters,
            maximumSurfaceDistanceMeters: maximumSurfaceDistanceMeters,
            surfaceScore: surfaceScore,
            score: score,
            guidance: guidance(score: score)
        )
    }

    var surfaceSamples: [SceneSurfaceSample] {
        Array(
            surfaceCells
                .map { cell, evidence in
                    (
                        evidence.lastObservationIndex,
                        SceneSurfaceSample(
                            id: "\(cell.x):\(cell.y):\(cell.z)",
                            position: evidence.positionTotal / Float(evidence.observationCount),
                            observationCount: evidence.observationCount,
                            viewAngleBinCount: evidence.viewDirections.count
                        )
                    )
                }
                .sorted { $0.0 < $1.0 }
                .suffix(180)
                .map(\.1)
        )
    }

    private func guidance(score: Float) -> String {
        if !disconnectedJumpAnchors.isEmpty {
            return "Bridge the path gap with overlapping frames before moving on"
        }
        if acceptedPoseCount < 8 {
            return "Build a slow connected pass with overlapping views"
        }
        if surfaceHitCount >= 8 && surfaceCells.count < 4 {
            return "Brush across more scene surfaces, not just one area"
        }
        if surfaceHitCount >= 8,
           surfaceCells.values.filter({ $0.viewDirections.count >= 2 }).count < 2 {
            return "Revisit amber surfaces from a side or diagonal angle"
        }
        if let minimumSurfaceDistanceMeters,
           let maximumSurfaceDistanceMeters,
           maximumSurfaceDistanceMeters - minimumSurfaceDistanceMeters > 4 {
            return "Keep a steadier distance while preserving overlap"
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

    private mutating func recordSurfacePoint(
        _ surfacePoint: SIMD3<Float>,
        cameraPosition: SIMD3<Float>
    ) {
        let distance = simd_distance(surfacePoint, cameraPosition)
        guard distance.isFinite, distance >= 0.2, distance <= 12 else { return }

        let cell = PositionCell(surfacePoint, cellSize: 0.35)
        let viewDirection = simd_normalize(cameraPosition - surfacePoint)
        observationIndex += 1
        var evidence = surfaceCells[cell] ?? SurfaceCellEvidence()
        evidence.positionTotal += surfacePoint
        evidence.observationCount += 1
        if evidence.viewDirections.allSatisfy({ simd_dot($0, viewDirection) < 0.94 }) {
            evidence.viewDirections.append(viewDirection)
        }
        evidence.lastObservationIndex = observationIndex
        surfaceCells[cell] = evidence
        surfaceHitCount += 1
        minimumSurfaceDistanceMeters = min(minimumSurfaceDistanceMeters ?? distance, distance)
        maximumSurfaceDistanceMeters = max(maximumSurfaceDistanceMeters ?? distance, distance)
    }

    private func sceneSurfaceScore(multiAngleSurfaceCellCount: Int) -> Float {
        guard surfaceHitCount > 0 else { return 0 }
        let hitProgress = min(1, Float(surfaceHitCount) / 16)
        let cellProgress = min(1, Float(surfaceCells.count) / 8)
        let angleProgress = min(1, Float(multiAngleSurfaceCellCount) / 4)
        return hitProgress * 0.2 + cellProgress * 0.45 + angleProgress * 0.35
    }
}

private struct SurfaceCellEvidence {
    var positionTotal = SIMD3<Float>.zero
    var observationCount = 0
    var viewDirections: [SIMD3<Float>] = []
    var lastObservationIndex = 0
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
