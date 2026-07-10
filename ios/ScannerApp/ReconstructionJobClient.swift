import Foundation

enum ReconstructionJobStatus: Decodable, Equatable, Sendable {
    case received
    case processing
    case validated
    case complete
    case failed
    case unknown(String)

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        switch value {
        case "received": self = .received
        case "processing": self = .processing
        case "validated": self = .validated
        case "complete": self = .complete
        case "failed": self = .failed
        default: self = .unknown(value)
        }
    }

    var title: String {
        switch self {
        case .received: return "Received"
        case .processing: return "Processing"
        case .validated: return "Validated"
        case .complete: return "Complete"
        case .failed: return "Failed"
        case .unknown(let value): return value.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    var systemImage: String {
        switch self {
        case .received: return "tray.and.arrow.down"
        case .processing: return "gearshape.2"
        case .validated: return "checkmark.shield"
        case .complete: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        case .unknown: return "questionmark.circle"
        }
    }
}

enum ReconstructionJobStage: Decodable, Equatable, Sendable {
    case received
    case queued
    case validating
    case reconstructing
    case meshing
    case exporting
    case finished
    case unknown(String)

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        switch value {
        case "received": self = .received
        case "queued": self = .queued
        case "validating": self = .validating
        case "reconstructing": self = .reconstructing
        case "meshing": self = .meshing
        case "exporting": self = .exporting
        case "finished": self = .finished
        default: self = .unknown(value)
        }
    }

    var title: String {
        switch self {
        case .received: return "Received"
        case .queued: return "Queued"
        case .validating: return "Validating"
        case .reconstructing: return "Reconstructing"
        case .meshing: return "Meshing"
        case .exporting: return "Exporting"
        case .finished: return "Finished"
        case .unknown(let value): return value.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
}

struct ReconstructionJob: Decodable, Identifiable, Equatable, Sendable {
    let scanID: String
    let status: ReconstructionJobStatus
    let stage: ReconstructionJobStage?
    let message: String?
    let imageCount: Int?
    let frameCount: Int?
    let outputs: [String: String]
    let createdAt: Date?
    let updatedAt: Date?
    let startedAt: Date?
    let finishedAt: Date?

    var id: String { scanID }

    enum CodingKeys: String, CodingKey {
        case scanID = "scan_id"
        case status
        case stage
        case message
        case imageCount = "image_count"
        case frameCount = "frame_count"
        case outputs
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        scanID = try container.decode(String.self, forKey: .scanID)
        status = try container.decode(ReconstructionJobStatus.self, forKey: .status)
        stage = try container.decodeIfPresent(ReconstructionJobStage.self, forKey: .stage)
        message = try container.decodeIfPresent(String.self, forKey: .message)
        imageCount = try container.decodeIfPresent(Int.self, forKey: .imageCount)
        frameCount = try container.decodeIfPresent(Int.self, forKey: .frameCount)
        outputs = try container.decodeIfPresent([String: String].self, forKey: .outputs) ?? [:]
        createdAt = try Self.decodeDate(in: container, forKey: .createdAt)
        updatedAt = try Self.decodeDate(in: container, forKey: .updatedAt)
        startedAt = try Self.decodeDate(in: container, forKey: .startedAt)
        finishedAt = try Self.decodeDate(in: container, forKey: .finishedAt)
    }

    private static func decodeDate(
        in container: KeyedDecodingContainer<CodingKeys>,
        forKey key: CodingKeys
    ) throws -> Date? {
        guard let value = try container.decodeIfPresent(String.self, forKey: key) else {
            return nil
        }

        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) {
            return date
        }

        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
}

protocol ReconstructionJobLoading {
    func listJobs(baseURL: URL, limit: Int) async throws -> [ReconstructionJob]
}

enum ReconstructionJobClientError: LocalizedError, Equatable {
    case invalidBaseURL
    case insecureNonLocalURL
    case invalidLimit
    case invalidResponse
    case httpStatus(Int)
    case invalidPayload

    var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            return "Enter a full HTTP or HTTPS backend URL."
        case .insecureNonLocalURL:
            return "HTTP is limited to private or local-network hosts. Use HTTPS for other hosts."
        case .invalidLimit:
            return "The job history limit must be between 1 and 200."
        case .invalidResponse:
            return "The backend returned an invalid response."
        case .httpStatus(let status):
            return "The backend returned HTTP status \(status)."
        case .invalidPayload:
            return "The backend job response could not be decoded."
        }
    }
}

struct HTTPReconstructionJobClient: ReconstructionJobLoading {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func listJobs(baseURL: URL, limit: Int = 50) async throws -> [ReconstructionJob] {
        guard (1...200).contains(limit) else {
            throw ReconstructionJobClientError.invalidLimit
        }
        guard let scheme = baseURL.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              let host = baseURL.host,
              baseURL.query == nil,
              baseURL.fragment == nil else {
            throw ReconstructionJobClientError.invalidBaseURL
        }
        if scheme == "http" && !Self.isLocalHost(host) {
            throw ReconstructionJobClientError.insecureNonLocalURL
        }

        let scansURL = baseURL.appendingPathComponent("scans", isDirectory: false)
        guard var components = URLComponents(url: scansURL, resolvingAgainstBaseURL: false) else {
            throw ReconstructionJobClientError.invalidBaseURL
        }
        components.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        guard let endpoint = components.url else {
            throw ReconstructionJobClientError.invalidBaseURL
        }

        var request = URLRequest(url: endpoint)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw ReconstructionJobClientError.invalidResponse
        }
        guard (200...299).contains(response.statusCode) else {
            throw ReconstructionJobClientError.httpStatus(response.statusCode)
        }

        do {
            return try JSONDecoder().decode([ReconstructionJob].self, from: data)
        } catch {
            throw ReconstructionJobClientError.invalidPayload
        }
    }

    private static func isLocalHost(_ rawHost: String) -> Bool {
        let host = rawHost
            .trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
            .lowercased()
        if host == "localhost"
            || host.hasSuffix(".localhost")
            || host.hasSuffix(".local")
            || host.hasSuffix(".home.arpa") {
            return true
        }

        let ipv4Parts = host.split(separator: ".", omittingEmptySubsequences: false)
        let octets = ipv4Parts.compactMap { Int($0) }
        if ipv4Parts.count == 4,
           octets.count == 4,
           octets.allSatisfy({ (0...255).contains($0) }) {
            return octets[0] == 10
                || octets[0] == 127
                || (octets[0] == 169 && octets[1] == 254)
                || (octets[0] == 172 && (16...31).contains(octets[1]))
                || (octets[0] == 192 && octets[1] == 168)
        }

        guard host.contains(":") else { return false }
        return host == "::1"
            || host.hasPrefix("fc")
            || host.hasPrefix("fd")
            || host.hasPrefix("fe8")
            || host.hasPrefix("fe9")
            || host.hasPrefix("fea")
            || host.hasPrefix("feb")
    }
}

struct InMemoryReconstructionJobClient: ReconstructionJobLoading {
    let jobs: [ReconstructionJob]

    func listJobs(baseURL: URL, limit: Int) async throws -> [ReconstructionJob] {
        guard (1...200).contains(limit) else {
            throw ReconstructionJobClientError.invalidLimit
        }
        return Array(jobs.prefix(limit))
    }
}
