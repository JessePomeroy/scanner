import Combine
import Foundation

struct ReconstructionRegion: Codable, Equatable, Sendable {
    let schemaVersion: String
    let shape: String
    let coordinateSystem: String
    let center: [Double]
    let extents: [Double]
    let orientationXYZW: [Double]
    let source: String
    let revision: Int

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case shape
        case coordinateSystem = "coordinate_system"
        case center
        case extents
        case orientationXYZW = "orientation_xyzw"
        case source
        case revision
    }

    static func userRegion(
        center: [Double],
        extents: [Double],
        eulerRadians: [Double],
        revision: Int
    ) throws -> ReconstructionRegion {
        guard eulerRadians.count == 3, eulerRadians.allSatisfy(\.isFinite) else {
            throw ReconstructionScopeClientError.invalidRegion
        }
        let halfX = eulerRadians[0] / 2
        let halfY = eulerRadians[1] / 2
        let halfZ = eulerRadians[2] / 2
        let cx = cos(halfX), sx = sin(halfX)
        let cy = cos(halfY), sy = sin(halfY)
        let cz = cos(halfZ), sz = sin(halfZ)
        let orientation = [
            (sx * cy * cz) - (cx * sy * sz),
            (cx * sy * cz) + (sx * cy * sz),
            (cx * cy * sz) - (sx * sy * cz),
            (cx * cy * cz) + (sx * sy * sz),
        ]
        let region = ReconstructionRegion(
            schemaVersion: "1.0",
            shape: "oriented_box",
            coordinateSystem: "colmap_reconstruction",
            center: center,
            extents: extents,
            orientationXYZW: orientation,
            source: "user_sparse_preview",
            revision: revision
        )
        guard region.isValid else {
            throw ReconstructionScopeClientError.invalidRegion
        }
        return region
    }

    var eulerRadians: [Double] {
        guard orientationXYZW.count == 4 else { return [0, 0, 0] }
        let x = orientationXYZW[0]
        let y = orientationXYZW[1]
        let z = orientationXYZW[2]
        let w = orientationXYZW[3]

        let roll = atan2(2 * ((w * x) + (y * z)), 1 - (2 * ((x * x) + (y * y))))
        let pitchTerm = 2 * ((w * y) - (z * x))
        let pitch = abs(pitchTerm) >= 1
            ? copysign(Double.pi / 2, pitchTerm)
            : asin(pitchTerm)
        let yaw = atan2(2 * ((w * z) + (x * y)), 1 - (2 * ((y * y) + (z * z))))
        return [roll, pitch, yaw]
    }

    var isValid: Bool {
        guard schemaVersion == "1.0",
              shape == "oriented_box",
              coordinateSystem == "colmap_reconstruction",
              ["user_sparse_preview", "automatic", "arkit_alignment", "imported"].contains(source),
              revision >= 1,
              center.count == 3,
              center.allSatisfy(\.isFinite),
              extents.count == 3,
              extents.allSatisfy({ $0.isFinite && $0 > 0 }),
              orientationXYZW.count == 4,
              orientationXYZW.allSatisfy(\.isFinite) else {
            return false
        }
        let norm = sqrt(orientationXYZW.reduce(0) { $0 + ($1 * $1) })
        return abs(norm - 1) <= 0.0001
    }

    func contains(x: Double, y: Double, z: Double) -> Bool {
        guard isValid else { return false }
        let translated = [x - center[0], y - center[1], z - center[2]]
        let quaternionVector = [
            -orientationXYZW[0],
            -orientationXYZW[1],
            -orientationXYZW[2],
        ]
        let cross = Self.cross(quaternionVector, translated)
        let doubledCross = cross.map { 2 * $0 }
        let rotatedCross = Self.cross(quaternionVector, doubledCross)
        let local = (0..<3).map {
            translated[$0] + (orientationXYZW[3] * doubledCross[$0]) + rotatedCross[$0]
        }
        return (0..<3).allSatisfy { abs(local[$0]) <= (extents[$0] / 2) + 1e-9 }
    }

    private static func cross(_ lhs: [Double], _ rhs: [Double]) -> [Double] {
        [
            (lhs[1] * rhs[2]) - (lhs[2] * rhs[1]),
            (lhs[2] * rhs[0]) - (lhs[0] * rhs[2]),
            (lhs[0] * rhs[1]) - (lhs[1] * rhs[0]),
        ]
    }
}

struct SparseCameraPreview: Decodable, Equatable, Sendable {
    struct Camera: Decodable, Equatable, Sendable {
        let imageID: Int
        let imageName: String
        let cameraID: Int
        let rotationWorldToCameraWXYZ: [Double]
        let translationWorldToCamera: [Double]
        let center: [Double]

        enum CodingKeys: String, CodingKey {
            case imageID = "image_id"
            case imageName = "image_name"
            case cameraID = "camera_id"
            case rotationWorldToCameraWXYZ = "rotation_world_to_camera_wxyz"
            case translationWorldToCamera = "translation_world_to_camera"
            case center
        }

        var isValid: Bool {
            guard imageID > 0
                && cameraID > 0
                && !imageName.isEmpty
                && rotationWorldToCameraWXYZ.count == 4
                && rotationWorldToCameraWXYZ.allSatisfy(\.isFinite)
                && translationWorldToCamera.count == 3
                && translationWorldToCamera.allSatisfy(\.isFinite)
                && center.count == 3
                && center.allSatisfy(\.isFinite) else {
                return false
            }
            let norm = sqrt(rotationWorldToCameraWXYZ.reduce(0) { $0 + ($1 * $1) })
            return abs(norm - 1) <= 0.0001
        }
    }

    let schemaVersion: String
    let coordinateSystem: String
    let cameraCount: Int
    let cameras: [Camera]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case coordinateSystem = "coordinate_system"
        case cameraCount = "camera_count"
        case cameras
    }

    var isValid: Bool {
        schemaVersion == "1.0"
            && coordinateSystem == "colmap_reconstruction"
            && cameraCount == cameras.count
            && (1...10_000).contains(cameraCount)
            && cameras.allSatisfy(\.isValid)
            && Set(cameras.map(\.imageID)).count == cameras.count
            && Set(cameras.map(\.imageName)).count == cameras.count
    }

    static func load(fileURL: URL) throws -> SparseCameraPreview {
        let data = try Data(contentsOf: fileURL, options: [.mappedIfSafe])
        guard data.count <= 16 * 1_024 * 1_024,
              let preview = try? JSONDecoder().decode(SparseCameraPreview.self, from: data),
              preview.isValid else {
            throw ReconstructionScopeClientError.invalidPayload
        }
        return preview
    }
}

protocol ReconstructionScopeAccessing {
    func loadRegion(scanID: String, baseURL: URL) async throws -> ReconstructionRegion?
    func saveRegion(
        _ region: ReconstructionRegion,
        scanID: String,
        baseURL: URL
    ) async throws -> ReconstructionRegion
}

protocol ReconstructionScopeTransport {
    func data(for request: URLRequest) async throws -> (Data, URLResponse)
}

struct URLSessionReconstructionScopeTransport: ReconstructionScopeTransport {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }
}

enum ReconstructionScopeClientError: LocalizedError, Equatable {
    case invalidScanID
    case invalidRegion
    case invalidResponse
    case httpStatus(Int)
    case staleRevision
    case invalidPayload

    var errorDescription: String? {
        switch self {
        case .invalidScanID:
            return "The reconstruction job has an invalid scan identifier."
        case .invalidRegion:
            return "The selected 3D region is invalid. Check that every size is greater than zero."
        case .invalidResponse:
            return "The backend returned an invalid scope response."
        case .httpStatus(let status):
            return "The backend returned HTTP status \(status) for this scope."
        case .staleRevision:
            return "This scope changed elsewhere. Reload it before saving again."
        case .invalidPayload:
            return "The backend scope response could not be decoded or validated."
        }
    }
}

struct HTTPReconstructionScopeClient: ReconstructionScopeAccessing {
    private struct Response: Decodable {
        let scanID: String
        let region: ReconstructionRegion

        enum CodingKeys: String, CodingKey {
            case scanID = "scan_id"
            case region
        }
    }

    private let transport: any ReconstructionScopeTransport

    init(transport: any ReconstructionScopeTransport = URLSessionReconstructionScopeTransport()) {
        self.transport = transport
    }

    func loadRegion(scanID: String, baseURL: URL) async throws -> ReconstructionRegion? {
        let endpoint = try Self.scopeURL(scanID: scanID, baseURL: baseURL)
        var request = Self.request(endpoint: endpoint)
        request.httpMethod = "GET"
        let (data, response) = try await transport.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw ReconstructionScopeClientError.invalidResponse
        }
        if response.statusCode == 404 { return nil }
        guard (200...299).contains(response.statusCode) else {
            throw ReconstructionScopeClientError.httpStatus(response.statusCode)
        }
        return try Self.decode(data, expectedScanID: scanID)
    }

    func saveRegion(
        _ region: ReconstructionRegion,
        scanID: String,
        baseURL: URL
    ) async throws -> ReconstructionRegion {
        guard region.isValid else {
            throw ReconstructionScopeClientError.invalidRegion
        }
        let endpoint = try Self.scopeURL(scanID: scanID, baseURL: baseURL)
        var request = Self.request(endpoint: endpoint)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(region)
        let (data, response) = try await transport.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw ReconstructionScopeClientError.invalidResponse
        }
        if response.statusCode == 409 {
            throw ReconstructionScopeClientError.staleRevision
        }
        guard (200...299).contains(response.statusCode) else {
            throw ReconstructionScopeClientError.httpStatus(response.statusCode)
        }
        return try Self.decode(data, expectedScanID: scanID)
    }

    private static func request(endpoint: URL) -> URLRequest {
        var request = URLRequest(url: endpoint)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private static func scopeURL(scanID: String, baseURL: URL) throws -> URL {
        guard isValidScanID(scanID) else {
            throw ReconstructionScopeClientError.invalidScanID
        }
        return try ReconstructionBackendEndpoint.scansURL(baseURL: baseURL)
            .appendingPathComponent(scanID, isDirectory: true)
            .appendingPathComponent("scope", isDirectory: false)
    }

    private static func isValidScanID(_ scanID: String) -> Bool {
        guard !scanID.isEmpty, scanID.utf8.count <= 128 else { return false }
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_."))
        return scanID.unicodeScalars.allSatisfy(allowed.contains)
            && scanID != "."
            && scanID != ".."
    }

    private static func decode(_ data: Data, expectedScanID: String) throws -> ReconstructionRegion {
        guard let response = try? JSONDecoder().decode(Response.self, from: data),
              response.scanID == expectedScanID,
              response.region.isValid else {
            throw ReconstructionScopeClientError.invalidPayload
        }
        return response.region
    }
}

@MainActor
final class ReconstructionScopeStore: ObservableObject {
    @Published private(set) var savedRegion: ReconstructionRegion?
    @Published private(set) var isLoading = false
    @Published private(set) var isSaving = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var confirmationMessage: String?

    private let client: any ReconstructionScopeAccessing

    init(client: any ReconstructionScopeAccessing) {
        self.client = client
    }

    func load(scanID: String, baseURLString: String) async {
        guard let baseURL = Self.baseURL(from: baseURLString) else {
            errorMessage = ReconstructionJobClientError.invalidBaseURL.localizedDescription
            return
        }
        isLoading = true
        errorMessage = nil
        confirmationMessage = nil
        defer { isLoading = false }
        do {
            savedRegion = try await client.loadRegion(scanID: scanID, baseURL: baseURL)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    @discardableResult
    func save(
        _ region: ReconstructionRegion,
        scanID: String,
        baseURLString: String
    ) async -> Bool {
        guard let baseURL = Self.baseURL(from: baseURLString) else {
            errorMessage = ReconstructionJobClientError.invalidBaseURL.localizedDescription
            return false
        }
        isSaving = true
        errorMessage = nil
        confirmationMessage = nil
        defer { isSaving = false }
        do {
            savedRegion = try await client.saveRegion(region, scanID: scanID, baseURL: baseURL)
            confirmationMessage = "Region revision \(region.revision) saved."
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }

    func clearMessages() {
        errorMessage = nil
        confirmationMessage = nil
    }

    private static func baseURL(from value: String) -> URL? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return URL(string: trimmed)
    }
}
