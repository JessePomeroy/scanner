import Foundation

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
    let whiteBalanceLocked: Bool
    let focusLocked: Bool

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
        case whiteBalanceLocked = "white_balance_locked"
        case focusLocked = "focus_locked"
    }
}

struct ScanSessionMetadata: Codable, Equatable {
    let scanId: String
    let createdAt: String
    let device: String
    let appVersion: String
    let scanMode: String
    let usesLidar: Bool
    let usesARKitMesh: Bool
    let imageCount: Int
    let depthFrameCount: Int
    let notes: String?

    enum CodingKeys: String, CodingKey {
        case scanId = "scan_id"
        case createdAt = "created_at"
        case device
        case appVersion = "app_version"
        case scanMode = "scan_mode"
        case usesLidar = "uses_lidar"
        case usesARKitMesh = "uses_arkit_mesh"
        case imageCount = "image_count"
        case depthFrameCount = "depth_frame_count"
        case notes
    }
}
