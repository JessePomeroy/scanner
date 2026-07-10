import Foundation

struct ReconstructionArtifact: Decodable, Identifiable, Equatable, Sendable {
    struct ID: Hashable, Sendable {
        let name: String
        let relativePath: String
    }

    let name: String
    let relativePath: String
    let filename: String
    let byteCount: Int64
    let mediaType: String

    var id: ID { ID(name: name, relativePath: relativePath) }

    var displayName: String {
        name.replacingOccurrences(of: "_", with: " ").capitalized
    }

    var supportsPointCloudPreview: Bool {
        (filename as NSString).pathExtension.lowercased() == "ply"
    }

    enum CodingKeys: String, CodingKey {
        case name
        case relativePath = "relative_path"
        case filename
        case byteCount = "byte_count"
        case mediaType = "media_type"
    }
}

struct DownloadedReconstructionArtifact: Equatable, Sendable {
    let artifact: ReconstructionArtifact
    let fileURL: URL
}

protocol ReconstructionArtifactAccessing {
    func listArtifacts(scanID: String, baseURL: URL) async throws -> [ReconstructionArtifact]
    func downloadArtifact(
        _ artifact: ReconstructionArtifact,
        scanID: String,
        baseURL: URL
    ) async throws -> DownloadedReconstructionArtifact
    func discardDownloadedArtifact(_ download: DownloadedReconstructionArtifact) async
}

protocol ReconstructionArtifactTransport {
    func data(for request: URLRequest) async throws -> (Data, URLResponse)
    func download(for request: URLRequest) async throws -> (URL, URLResponse)
}

struct URLSessionReconstructionArtifactTransport: ReconstructionArtifactTransport {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }

    func download(for request: URLRequest) async throws -> (URL, URLResponse) {
        try await session.download(for: request)
    }
}

enum ReconstructionArtifactClientError: LocalizedError, Equatable {
    case invalidScanID
    case invalidArtifact
    case invalidResponse
    case httpStatus(Int)
    case invalidPayload
    case unableToStoreDownload
    case downloadedSizeMismatch

    var errorDescription: String? {
        switch self {
        case .invalidScanID:
            return "The reconstruction job has an invalid scan identifier."
        case .invalidArtifact:
            return "The backend returned an unsafe or invalid result description."
        case .invalidResponse:
            return "The backend returned an invalid result response."
        case .httpStatus(let status):
            return "The backend returned HTTP status \(status) for this result."
        case .invalidPayload:
            return "The backend result list could not be decoded."
        case .unableToStoreDownload:
            return "The downloaded result could not be stored for sharing."
        case .downloadedSizeMismatch:
            return "The downloaded result size did not match the backend manifest."
        }
    }
}

struct HTTPReconstructionArtifactClient: ReconstructionArtifactAccessing {
    private let transport: ReconstructionArtifactTransport

    init(
        transport: ReconstructionArtifactTransport = URLSessionReconstructionArtifactTransport()
    ) {
        ReconstructionArtifactFiles.removeAbandonedDownloads()
        self.transport = transport
    }

    func listArtifacts(scanID: String, baseURL: URL) async throws -> [ReconstructionArtifact] {
        let endpoint = try Self.artifactsURL(scanID: scanID, baseURL: baseURL)
        var request = URLRequest(url: endpoint)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response) = try await transport.data(for: request)
        try Self.validate(response)

        let artifacts: [ReconstructionArtifact]
        do {
            artifacts = try JSONDecoder().decode([ReconstructionArtifact].self, from: data)
        } catch {
            throw ReconstructionArtifactClientError.invalidPayload
        }
        guard artifacts.allSatisfy(Self.isValid),
              Set(artifacts.map(\.id)).count == artifacts.count,
              Set(artifacts.map(\.relativePath)).count == artifacts.count else {
            throw ReconstructionArtifactClientError.invalidArtifact
        }
        return artifacts
    }

    func downloadArtifact(
        _ artifact: ReconstructionArtifact,
        scanID: String,
        baseURL: URL
    ) async throws -> DownloadedReconstructionArtifact {
        guard Self.isValid(artifact) else {
            throw ReconstructionArtifactClientError.invalidArtifact
        }
        let endpoint = try Self.fileURL(
            scanID: scanID,
            relativePath: artifact.relativePath,
            baseURL: baseURL
        )
        var request = URLRequest(url: endpoint)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 30 * 60
        request.setValue(artifact.mediaType, forHTTPHeaderField: "Accept")

        let (temporaryURL, response) = try await transport.download(for: request)
        defer {
            try? FileManager.default.removeItem(at: temporaryURL)
        }
        try Task.checkCancellation()
        try Self.validate(response)

        let ownedURL = try await ReconstructionArtifactFiles.store(
            temporaryURL: temporaryURL,
            filename: artifact.filename,
            expectedByteCount: artifact.byteCount
        )
        return DownloadedReconstructionArtifact(
            artifact: artifact,
            fileURL: ownedURL
        )
    }

    func discardDownloadedArtifact(_ download: DownloadedReconstructionArtifact) async {
        await ReconstructionArtifactFiles.discard(download.fileURL)
    }

    private static func artifactsURL(scanID: String, baseURL: URL) throws -> URL {
        guard isValidScanID(scanID) else {
            throw ReconstructionArtifactClientError.invalidScanID
        }
        return try ReconstructionBackendEndpoint.scansURL(baseURL: baseURL)
            .appendingPathComponent(scanID, isDirectory: true)
            .appendingPathComponent("artifacts", isDirectory: false)
    }

    private static func fileURL(
        scanID: String,
        relativePath: String,
        baseURL: URL
    ) throws -> URL {
        guard isValidScanID(scanID),
              let parts = validRelativePathParts(relativePath) else {
            throw ReconstructionArtifactClientError.invalidArtifact
        }
        var endpoint = try ReconstructionBackendEndpoint.scansURL(baseURL: baseURL)
            .appendingPathComponent(scanID, isDirectory: true)
            .appendingPathComponent("files", isDirectory: true)
        for part in parts {
            endpoint.appendPathComponent(part, isDirectory: false)
        }
        return endpoint
    }

    private static func validate(_ response: URLResponse) throws {
        guard let response = response as? HTTPURLResponse else {
            throw ReconstructionArtifactClientError.invalidResponse
        }
        guard (200...299).contains(response.statusCode) else {
            throw ReconstructionArtifactClientError.httpStatus(response.statusCode)
        }
    }

    private static func isValid(_ artifact: ReconstructionArtifact) -> Bool {
        guard !artifact.name.isEmpty,
              artifact.name.unicodeScalars.count <= 128,
              !artifact.name.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              artifact.byteCount >= 0,
              !artifact.mediaType.isEmpty,
              artifact.mediaType.unicodeScalars.count <= 128,
              !artifact.mediaType.unicodeScalars.contains(
                where: CharacterSet.controlCharacters.contains
              ),
              let parts = validRelativePathParts(artifact.relativePath),
              validFilename(artifact.filename),
              parts.last == artifact.filename else {
            return false
        }
        return true
    }

    private static func validFilename(_ filename: String) -> Bool {
        !filename.isEmpty
            && filename != "."
            && filename != ".."
            && filename.unicodeScalars.count <= 255
            && !filename.contains("/")
            && !filename.contains("\\")
            && !filename.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
    }

    private static func validRelativePathParts(_ relativePath: String) -> [String]? {
        guard !relativePath.isEmpty,
              !relativePath.hasPrefix("/"),
              !relativePath.contains("\\"),
              !relativePath.unicodeScalars.contains(
                where: CharacterSet.controlCharacters.contains
              ) else {
            return nil
        }
        let parts = relativePath.split(separator: "/", omittingEmptySubsequences: false)
            .map(String.init)
        guard !parts.isEmpty,
              parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
            return nil
        }
        return parts
    }

    private static func isValidScanID(_ scanID: String) -> Bool {
        let scalars = Array(scanID.unicodeScalars)
        guard !scalars.isEmpty,
              scalars.count <= 255,
              isASCIIAlphaNumeric(scalars[0]) else {
            return false
        }
        return scalars.dropFirst().allSatisfy { scalar in
            isASCIIAlphaNumeric(scalar) || scalar == "." || scalar == "_" || scalar == "-"
        }
    }

    private static func isASCIIAlphaNumeric(_ scalar: UnicodeScalar) -> Bool {
        (48...57).contains(scalar.value)
            || (65...90).contains(scalar.value)
            || (97...122).contains(scalar.value)
    }
}

struct InMemoryReconstructionArtifactClient: ReconstructionArtifactAccessing {
    let listHandler: (String, URL) async throws -> [ReconstructionArtifact]
    let downloadHandler: (
        ReconstructionArtifact,
        String,
        URL
    ) async throws -> DownloadedReconstructionArtifact
    let discardHandler: (DownloadedReconstructionArtifact) async -> Void

    func listArtifacts(scanID: String, baseURL: URL) async throws -> [ReconstructionArtifact] {
        try await listHandler(scanID, baseURL)
    }

    func downloadArtifact(
        _ artifact: ReconstructionArtifact,
        scanID: String,
        baseURL: URL
    ) async throws -> DownloadedReconstructionArtifact {
        try await downloadHandler(artifact, scanID, baseURL)
    }

    func discardDownloadedArtifact(_ download: DownloadedReconstructionArtifact) async {
        await discardHandler(download)
    }
}

private enum ReconstructionArtifactFiles {
    private static let rootDirectory = FileManager.default.temporaryDirectory
        .appendingPathComponent("scanner-result-downloads", isDirectory: true)

    private static let removeAbandonedDownloadsOnce: Void = {
        try? FileManager.default.removeItem(at: rootDirectory)
        try? FileManager.default.createDirectory(
            at: rootDirectory,
            withIntermediateDirectories: true
        )
    }()

    static func removeAbandonedDownloads() {
        _ = removeAbandonedDownloadsOnce
    }

    static func store(
        temporaryURL: URL,
        filename: String,
        expectedByteCount: Int64
    ) async throws -> URL {
        let storeTask = Task.detached(priority: .utility) {
            try Task.checkCancellation()
            let fileManager = FileManager.default
            let directory = rootDirectory.appendingPathComponent(
                UUID().uuidString,
                isDirectory: true
            )
            do {
                try fileManager.createDirectory(
                    at: directory,
                    withIntermediateDirectories: false
                )
                let destination = directory.appendingPathComponent(
                    filename,
                    isDirectory: false
                )
                try fileManager.moveItem(at: temporaryURL, to: destination)
                try Task.checkCancellation()
                let values = try destination.resourceValues(
                    forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey]
                )
                guard values.isRegularFile == true,
                      values.isSymbolicLink != true,
                      let fileSize = values.fileSize,
                      Int64(fileSize) == expectedByteCount else {
                    if values.isRegularFile != true || values.isSymbolicLink == true {
                        throw ReconstructionArtifactClientError.unableToStoreDownload
                    }
                    throw ReconstructionArtifactClientError.downloadedSizeMismatch
                }
                return destination
            } catch {
                try? fileManager.removeItem(at: directory)
                if error is CancellationError {
                    throw CancellationError()
                }
                if let error = error as? ReconstructionArtifactClientError {
                    throw error
                }
                throw ReconstructionArtifactClientError.unableToStoreDownload
            }
        }
        return try await withTaskCancellationHandler {
            try await storeTask.value
        } onCancel: {
            storeTask.cancel()
        }
    }

    static func discard(_ fileURL: URL) async {
        guard let directory = ownedDownloadDirectory(for: fileURL) else { return }
        _ = await Task.detached(priority: .utility) {
            try? FileManager.default.removeItem(at: directory)
        }.value
    }

    private static func ownedDownloadDirectory(for fileURL: URL) -> URL? {
        guard fileURL.isFileURL else { return nil }
        let standardizedRoot = rootDirectory.standardizedFileURL
        let directory = fileURL.deletingLastPathComponent().standardizedFileURL
        guard directory.deletingLastPathComponent() == standardizedRoot,
              UUID(uuidString: directory.lastPathComponent) != nil else {
            return nil
        }
        return directory
    }
}
