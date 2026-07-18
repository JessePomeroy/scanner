import Foundation

enum MaskReviewState: String, Decodable, Equatable, Sendable {
    case awaitingReview = "awaiting_review"
    case needsCorrection = "needs_correction"
    case approved
    case rejected
}

struct MaskReviewIssue: Decodable, Equatable, Sendable, Identifiable {
    let code: String
    let message: String
    let frameID: Int?
    let frameIDs: [Int]?

    var id: String {
        "\(code):\(frameID.map(String.init) ?? frameIDs?.map(String.init).joined(separator: ",") ?? "all")"
    }

    enum CodingKeys: String, CodingKey {
        case code
        case message
        case frameID = "frame_id"
        case frameIDs = "frame_ids"
    }
}

struct MaskReviewQuality: Decodable, Equatable, Sendable {
    let blockingIssueCount: Int
    let warningCount: Int
    let blockingIssues: [MaskReviewIssue]
    let warnings: [MaskReviewIssue]

    enum CodingKeys: String, CodingKey {
        case blockingIssueCount = "blocking_issue_count"
        case warningCount = "warning_count"
        case blockingIssues = "blocking_issues"
        case warnings
    }

    var isValid: Bool {
        blockingIssueCount >= 0
            && warningCount >= 0
            && blockingIssueCount == blockingIssues.count
            && warningCount == warnings.count
            && Set(blockingIssues.map(\.id)).count == blockingIssues.count
            && Set(warnings.map(\.id)).count == warnings.count
            && (blockingIssues + warnings).allSatisfy {
                !$0.code.isEmpty && !$0.message.isEmpty
            }
    }
}

struct MaskReviewReport: Decodable, Equatable, Sendable {
    let schemaVersion: String
    let state: MaskReviewState
    let generator: String
    let sourceAuthoringRevision: Int
    let frameCount: Int
    let reviewIndices: [Int]
    let reviewMasks: [String]
    let quality: MaskReviewQuality

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case state
        case generator
        case sourceAuthoringRevision = "source_authoring_revision"
        case frameCount = "frame_count"
        case reviewIndices = "review_indices"
        case reviewMasks = "review_masks"
        case quality
    }

    var isValid: Bool {
        schemaVersion == "1.0"
            && !generator.isEmpty
            && sourceAuthoringRevision >= 1
            && frameCount >= 1
            && (1...5).contains(reviewIndices.count)
            && reviewIndices.count == reviewMasks.count
            && reviewIndices == reviewIndices.sorted()
            && Set(reviewIndices).count == reviewIndices.count
            && reviewIndices.allSatisfy { (0..<frameCount).contains($0) }
            && Set(reviewMasks).count == reviewMasks.count
            && reviewMasks.allSatisfy(Self.isValidReviewPath)
            && quality.isValid
            && stateMatchesQuality
    }

    private var stateMatchesQuality: Bool {
        switch state {
        case .awaitingReview, .approved:
            return quality.blockingIssueCount == 0
        case .needsCorrection:
            return quality.blockingIssueCount > 0
        case .rejected:
            return true
        }
    }

    private static func isValidReviewPath(_ value: String) -> Bool {
        let parts = value.split(separator: "/", omittingEmptySubsequences: false)
        return parts.count == 3
            && value.utf8.count <= 512
            && parts[0] == "masks"
            && parts[1] == "review"
            && !parts[2].isEmpty
            && parts[2].hasSuffix(".png")
            && !value.contains("\\")
            && !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
            && !parts.contains(".")
            && !parts.contains("..")
    }
}

protocol MaskReviewAccessing {
    func loadReview(scanID: String, baseURL: URL) async throws -> MaskReviewReport
    func approve(scanID: String, baseURL: URL) async throws -> ReconstructionJob
    func reject(scanID: String, baseURL: URL) async throws -> ReconstructionJob
}

protocol MaskReviewTransport {
    func data(for request: URLRequest) async throws -> (Data, URLResponse)
}

struct URLSessionMaskReviewTransport: MaskReviewTransport {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }
}

enum MaskReviewClientError: LocalizedError, Equatable {
    case invalidScanID
    case invalidResponse
    case httpStatus(Int)
    case invalidPayload

    var errorDescription: String? {
        switch self {
        case .invalidScanID:
            return "The mask-review job has an invalid scan identifier."
        case .invalidResponse:
            return "The backend returned an invalid mask-review response."
        case .httpStatus(let status):
            return "The backend returned HTTP status \(status) for mask review."
        case .invalidPayload:
            return "The backend mask-review evidence could not be decoded or validated."
        }
    }
}

struct HTTPMaskReviewClient: MaskReviewAccessing {
    private let transport: any MaskReviewTransport

    init(transport: any MaskReviewTransport = URLSessionMaskReviewTransport()) {
        self.transport = transport
    }

    func loadReview(scanID: String, baseURL: URL) async throws -> MaskReviewReport {
        let endpoint = try Self.reviewURL(scanID: scanID, baseURL: baseURL)
        let (data, response) = try await transport.data(for: Self.request(endpoint, method: "GET"))
        try Self.validate(response)
        guard let report = try? JSONDecoder().decode(MaskReviewReport.self, from: data),
              report.isValid else {
            throw MaskReviewClientError.invalidPayload
        }
        return report
    }

    func approve(scanID: String, baseURL: URL) async throws -> ReconstructionJob {
        try await decide("approve", scanID: scanID, baseURL: baseURL)
    }

    func reject(scanID: String, baseURL: URL) async throws -> ReconstructionJob {
        try await decide("reject", scanID: scanID, baseURL: baseURL)
    }

    private func decide(
        _ decision: String,
        scanID: String,
        baseURL: URL
    ) async throws -> ReconstructionJob {
        let endpoint = try Self.reviewURL(scanID: scanID, baseURL: baseURL)
            .appendingPathComponent(decision, isDirectory: false)
        let (data, response) = try await transport.data(for: Self.request(endpoint, method: "POST"))
        try Self.validate(response)
        guard let job = try? JSONDecoder().decode(ReconstructionJob.self, from: data),
              job.scanID == scanID else {
            throw MaskReviewClientError.invalidPayload
        }
        return job
    }

    private static func request(_ endpoint: URL, method: String) -> URLRequest {
        var request = URLRequest(url: endpoint)
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private static func reviewURL(scanID: String, baseURL: URL) throws -> URL {
        guard isValidScanID(scanID) else {
            throw MaskReviewClientError.invalidScanID
        }
        return try ReconstructionBackendEndpoint.scansURL(baseURL: baseURL)
            .appendingPathComponent(scanID, isDirectory: true)
            .appendingPathComponent("mask-review", isDirectory: false)
    }

    private static func isValidScanID(_ scanID: String) -> Bool {
        guard !scanID.isEmpty, scanID.utf8.count <= 128 else { return false }
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_."))
        return scanID != "."
            && scanID != ".."
            && scanID.unicodeScalars.allSatisfy(allowed.contains)
    }

    private static func validate(_ response: URLResponse) throws {
        guard let response = response as? HTTPURLResponse else {
            throw MaskReviewClientError.invalidResponse
        }
        guard (200...299).contains(response.statusCode) else {
            throw MaskReviewClientError.httpStatus(response.statusCode)
        }
    }
}

struct InMemoryMaskReviewClient: MaskReviewAccessing {
    let loadHandler: (String, URL) async throws -> MaskReviewReport
    let approveHandler: (String, URL) async throws -> ReconstructionJob
    let rejectHandler: (String, URL) async throws -> ReconstructionJob

    func loadReview(scanID: String, baseURL: URL) async throws -> MaskReviewReport {
        try await loadHandler(scanID, baseURL)
    }

    func approve(scanID: String, baseURL: URL) async throws -> ReconstructionJob {
        try await approveHandler(scanID, baseURL)
    }

    func reject(scanID: String, baseURL: URL) async throws -> ReconstructionJob {
        try await rejectHandler(scanID, baseURL)
    }
}
