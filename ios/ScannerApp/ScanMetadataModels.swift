import Foundation

enum ScanMode: String, CaseIterable, Codable, Identifiable {
    case object = "object_scan"
    case scene = "scene_scan"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .object:
            return "Object"
        case .scene:
            return "Scene"
        }
    }
}

enum ObjectRadiusPreset: Float, CaseIterable, Codable, Identifiable {
    case small = 0.75
    case medium = 1.5
    case large = 3.0

    var id: Float { rawValue }

    var title: String {
        switch self {
        case .small:
            return "0.75m"
        case .medium:
            return "1.5m"
        case .large:
            return "3m"
        }
    }
}

struct CapturedFrameMetadata: Codable, Equatable {
    let id: Int
    let imagePath: String
    let depthPath: String?
    let timestamp: Double
    let cameraTransform: [[Float]]
    let intrinsics: [[Float]]
    let resolution: [Int]
    let trackingState: String
    let blurScore: Float
    let exposureDuration: Double?
    let iso: Float?
    let exposureTargetOffset: Float?
    let ambientIntensity: Float?
    let ambientColorTemperature: Float?
    let whiteBalanceLocked: Bool
    let focusLocked: Bool
    let movementDeltaMeters: Float?
    let rotationDeltaDegrees: Float?
    let secondsSincePreviousFrame: Double?
    let movementSpeedMetersPerSecond: Float?

    enum CodingKeys: String, CodingKey {
        case id
        case imagePath = "image"
        case depthPath = "depth"
        case timestamp
        case cameraTransform = "camera_transform"
        case intrinsics
        case resolution
        case trackingState = "tracking_state"
        case blurScore = "blur_score"
        case exposureDuration = "exposure_duration"
        case iso
        case exposureTargetOffset = "exposure_target_offset"
        case ambientIntensity = "ambient_intensity"
        case ambientColorTemperature = "ambient_color_temperature"
        case whiteBalanceLocked = "white_balance_locked"
        case focusLocked = "focus_locked"
        case movementDeltaMeters = "movement_delta_meters"
        case rotationDeltaDegrees = "rotation_delta_degrees"
        case secondsSincePreviousFrame = "seconds_since_previous_frame"
        case movementSpeedMetersPerSecond = "movement_speed_meters_per_second"
    }
}

struct MotionSampleMetadata: Codable, Equatable {
    let timestamp: Double
    let attitudeQuaternion: [Double]
    let rotationRate: [Double]
    let gravity: [Double]
    let userAcceleration: [Double]

    enum CodingKeys: String, CodingKey {
        case timestamp
        case attitudeQuaternion = "attitude_quaternion"
        case rotationRate = "rotation_rate"
        case gravity
        case userAcceleration = "user_acceleration"
    }
}

struct VideoCaptureMetadata: Codable, Equatable {
    let path: String
    let capturedAt: String
    let durationSeconds: Double?
    let frameRate: Double?
    let resolution: [Int]?
    let codec: String?
    let includesAudio: Bool

    enum CodingKeys: String, CodingKey {
        case path
        case capturedAt = "captured_at"
        case durationSeconds = "duration_seconds"
        case frameRate = "frame_rate"
        case resolution
        case codec
        case includesAudio = "includes_audio"
    }
}

struct ScanSessionMetadata: Codable, Equatable {
    let scanId: String
    let createdAt: String
    let device: String
    let appVersion: String
    let buildVersion: String
    let scanMode: String
    let usesLidar: Bool
    let usesARKitMesh: Bool
    let imageCount: Int
    let depthFrameCount: Int
    let imuSampleCount: Int
    let videoCount: Int
    let rejectedFrameCount: Int
    let rejectedTrackingCount: Int
    let rejectedBlurCount: Int
    let rejectedMotionCount: Int
    let averageBlurScore: Float?
    let minimumBlurScore: Float?
    let maximumMovementSpeedMetersPerSecond: Float?
    let captureDurationSeconds: Double?
    let objectCenterWorld: [Float]?
    let objectRadiusMeters: Float?
    let sceneCoverage: SceneCoverageMetadata?
    let notes: String?

    enum CodingKeys: String, CodingKey {
        case scanId = "scan_id"
        case createdAt = "created_at"
        case device
        case appVersion = "app_version"
        case buildVersion = "build_version"
        case scanMode = "scan_mode"
        case usesLidar = "uses_lidar"
        case usesARKitMesh = "uses_arkit_mesh"
        case imageCount = "image_count"
        case depthFrameCount = "depth_frame_count"
        case imuSampleCount = "imu_sample_count"
        case videoCount = "video_count"
        case rejectedFrameCount = "rejected_frame_count"
        case rejectedTrackingCount = "rejected_tracking_count"
        case rejectedBlurCount = "rejected_blur_count"
        case rejectedMotionCount = "rejected_motion_count"
        case averageBlurScore = "average_blur_score"
        case minimumBlurScore = "minimum_blur_score"
        case maximumMovementSpeedMetersPerSecond = "maximum_movement_speed_meters_per_second"
        case captureDurationSeconds = "capture_duration_seconds"
        case objectCenterWorld = "object_center_world"
        case objectRadiusMeters = "object_radius_meters"
        case sceneCoverage = "scene_coverage"
        case notes
    }
}

struct SceneCoverageMetadata: Codable, Equatable {
    let schemaVersion: String
    let acceptedPoseCount: Int
    let uniquePositionCellCount: Int
    let headingBinCount: Int
    let elevationBinCount: Int
    let pathLengthMeters: Float
    let score: Float

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case acceptedPoseCount = "accepted_pose_count"
        case uniquePositionCellCount = "unique_position_cell_count"
        case headingBinCount = "heading_bin_count"
        case elevationBinCount = "elevation_bin_count"
        case pathLengthMeters = "path_length_meters"
        case score
    }
}

struct ScanPackageManifest: Codable, Equatable {
    let schemaVersion: String
    let scanId: String
    let scanMode: String
    let appVersion: String
    let buildVersion: String
    let imageCount: Int
    let depthFrameCount: Int
    let imuSampleCount: Int
    let videoCount: Int
    let usesLidar: Bool
    let usesARKitMesh: Bool
    let usesVideo: Bool
    let createdAt: String
    let limitations: [String]
    var reconstructionScope: ReconstructionScopeManifest? = nil

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case scanId = "scan_id"
        case scanMode = "scan_mode"
        case appVersion = "app_version"
        case buildVersion = "build_version"
        case imageCount = "image_count"
        case depthFrameCount = "depth_frame_count"
        case imuSampleCount = "imu_sample_count"
        case videoCount = "video_count"
        case usesLidar = "uses_lidar"
        case usesARKitMesh = "uses_arkit_mesh"
        case usesVideo = "uses_video"
        case createdAt = "created_at"
        case limitations
        case reconstructionScope = "reconstruction_scope"
    }
}

struct ReconstructionScopeManifest: Codable, Equatable {
    let schemaVersion: String
    let mode: String
    let maskSpace: String
    let maskConvention: String
    let maskCount: Int

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case mode
        case maskSpace = "mask_space"
        case maskConvention = "mask_convention"
        case maskCount = "mask_count"
    }
}

struct ScanExportSummary: Equatable {
    let scanId: String
    let zipFileName: String
    let scanModeTitle: String
    let acceptedFrameCount: Int
    let rejectedFrameCount: Int
    let videoCount: Int
    let averageBlurScore: Float?
    let minimumBlurScore: Float?
    let maximumMovementSpeedMetersPerSecond: Float?
    let captureDurationSeconds: Double?
    let objectRadiusMeters: Float?
    let objectCenterWasSet: Bool
    let sceneCoverageScore: Float?
}
