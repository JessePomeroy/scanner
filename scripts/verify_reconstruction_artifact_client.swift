import Foundation

struct StubReconstructionArtifactTransport: ReconstructionArtifactTransport {
    let dataHandler: (URLRequest) async throws -> (Data, URLResponse)
    let downloadHandler: (URLRequest) async throws -> (URL, URLResponse)

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await dataHandler(request)
    }

    func download(for request: URLRequest) async throws -> (URL, URLResponse) {
        try await downloadHandler(request)
    }
}

actor ArtifactDiscardCapture {
    private(set) var discardedURLs: [URL] = []

    func record(_ url: URL) {
        discardedURLs.append(url)
    }

    func snapshot() -> [URL] {
        discardedURLs
    }
}

enum ArtifactVerificationError: Error {
    case unexpectedRequest
    case assertionFailed(String)
}

@main
struct VerifyReconstructionArtifactClient {
    @MainActor
    static func main() async throws {
        let fileManager = FileManager.default
        let ownedRoot = fileManager.temporaryDirectory
            .appendingPathComponent("scanner-result-downloads", isDirectory: true)
        try? fileManager.createDirectory(at: ownedRoot, withIntermediateDirectories: true)
        let orphanDirectory = ownedRoot.appendingPathComponent(
            UUID().uuidString,
            isDirectory: true
        )
        try fileManager.createDirectory(at: orphanDirectory, withIntermediateDirectories: false)
        try Data("orphan".utf8).write(
            to: orphanDirectory.appendingPathComponent("orphan.ply")
        )

        let artifactBytes = Data("downloaded mesh".utf8)
        let payload = Data(
            """
            [
              {
                "name": "colmap_output",
                "relative_path": "dense/mesh result.ply",
                "filename": "mesh result.ply",
                "byte_count": \(artifactBytes.count),
                "media_type": "application/octet-stream"
              }
            ]
            """.utf8
        )
        let transport = StubReconstructionArtifactTransport(
            dataHandler: { request in
                try require(
                    request.url?.path == "/api/scans/scan-1/artifacts",
                    "Expected typed artifact manifest URL"
                )
                let response = HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )!
                return (payload, response)
            },
            downloadHandler: { request in
                let path = URLComponents(
                    url: request.url!,
                    resolvingAgainstBaseURL: false
                )?.percentEncodedPath
                try require(
                    path == "/api/scans/scan-1/files/dense/mesh%20result.ply",
                    "Expected encoded artifact download URL"
                )
                let temporaryURL = fileManager.temporaryDirectory
                    .appendingPathComponent("artifact-transport-\(UUID().uuidString).tmp")
                try artifactBytes.write(to: temporaryURL)
                let response = HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/octet-stream"]
                )!
                return (temporaryURL, response)
            }
        )
        let client = HTTPReconstructionArtifactClient(transport: transport)
        try require(
            !fileManager.fileExists(atPath: orphanDirectory.path),
            "Expected abandoned result cleanup when the first client starts"
        )

        let baseURL = URL(string: "http://127.0.0.1:8000/api")!
        let artifacts = try await client.listArtifacts(scanID: "scan-1", baseURL: baseURL)
        try require(artifacts.count == 1, "Expected one decoded artifact")
        try require(artifacts[0].displayName == "Colmap Output", "Expected display name")

        let download = try await client.downloadArtifact(
            artifacts[0],
            scanID: "scan-1",
            baseURL: baseURL
        )
        try require(download.fileURL.lastPathComponent == "mesh result.ply", "Expected filename")
        let downloadedBytes = try Data(contentsOf: download.fileURL)
        try require(
            downloadedBytes == artifactBytes,
            "Expected downloaded file bytes"
        )
        let ownedDirectory = download.fileURL.deletingLastPathComponent()
        await client.discardDownloadedArtifact(download)
        try require(
            !fileManager.fileExists(atPath: ownedDirectory.path),
            "Expected owned download cleanup"
        )

        do {
            _ = try await client.listArtifacts(scanID: "../unsafe", baseURL: baseURL)
            throw ArtifactVerificationError.assertionFailed("Expected invalid scan ID")
        } catch ReconstructionArtifactClientError.invalidScanID {
            // Expected.
        }

        let unsafeArtifact = ReconstructionArtifact(
            name: "unsafe",
            relativePath: "../secret.ply",
            filename: "secret.ply",
            byteCount: 1,
            mediaType: "application/octet-stream"
        )
        do {
            _ = try await client.downloadArtifact(
                unsafeArtifact,
                scanID: "scan-1",
                baseURL: baseURL
            )
            throw ArtifactVerificationError.assertionFailed("Expected unsafe artifact rejection")
        } catch ReconstructionArtifactClientError.invalidArtifact {
            // Expected.
        }

        let invalidManifestTransport = StubReconstructionArtifactTransport(
            dataHandler: { request in
                let invalidPayload = Data(
                    """
                    [{
                      "name": "bad",
                      "relative_path": "dense/file.ply",
                      "filename": "different.ply",
                      "byte_count": -1,
                      "media_type": "application/octet-stream"
                    }]
                    """.utf8
                )
                return (
                    invalidPayload,
                    HTTPURLResponse(
                        url: request.url!,
                        statusCode: 200,
                        httpVersion: nil,
                        headerFields: nil
                    )!
                )
            },
            downloadHandler: { _ in throw ArtifactVerificationError.unexpectedRequest }
        )
        do {
            _ = try await HTTPReconstructionArtifactClient(
                transport: invalidManifestTransport
            ).listArtifacts(scanID: "scan-1", baseURL: baseURL)
            throw ArtifactVerificationError.assertionFailed("Expected invalid manifest")
        } catch ReconstructionArtifactClientError.invalidArtifact {
            // Expected.
        }

        let mismatchTransport = StubReconstructionArtifactTransport(
            dataHandler: { _ in throw ArtifactVerificationError.unexpectedRequest },
            downloadHandler: { request in
                let temporaryURL = fileManager.temporaryDirectory
                    .appendingPathComponent("artifact-mismatch-\(UUID().uuidString).tmp")
                try Data("short".utf8).write(to: temporaryURL)
                return (
                    temporaryURL,
                    HTTPURLResponse(
                        url: request.url!,
                        statusCode: 200,
                        httpVersion: nil,
                        headerFields: nil
                    )!
                )
            }
        )
        do {
            _ = try await HTTPReconstructionArtifactClient(
                transport: mismatchTransport
            ).downloadArtifact(artifacts[0], scanID: "scan-1", baseURL: baseURL)
            throw ArtifactVerificationError.assertionFailed("Expected size mismatch")
        } catch ReconstructionArtifactClientError.downloadedSizeMismatch {
            // Expected.
        }
        let ownedChildren = try fileManager.contentsOfDirectory(
            at: ownedRoot,
            includingPropertiesForKeys: nil
        )
        try require(ownedChildren.isEmpty, "Expected failed download cleanup")

        let externalSentinel = fileManager.temporaryDirectory
            .appendingPathComponent("artifact-external-\(UUID().uuidString).ply")
        try artifactBytes.write(to: externalSentinel)
        defer {
            try? fileManager.removeItem(at: externalSentinel)
        }
        let symlinkTransport = StubReconstructionArtifactTransport(
            dataHandler: { _ in throw ArtifactVerificationError.unexpectedRequest },
            downloadHandler: { request in
                let temporaryURL = fileManager.temporaryDirectory
                    .appendingPathComponent("artifact-link-\(UUID().uuidString).tmp")
                try fileManager.createSymbolicLink(
                    at: temporaryURL,
                    withDestinationURL: externalSentinel
                )
                return (
                    temporaryURL,
                    HTTPURLResponse(
                        url: request.url!,
                        statusCode: 200,
                        httpVersion: nil,
                        headerFields: nil
                    )!
                )
            }
        )
        do {
            _ = try await HTTPReconstructionArtifactClient(
                transport: symlinkTransport
            ).downloadArtifact(artifacts[0], scanID: "scan-1", baseURL: baseURL)
            throw ArtifactVerificationError.assertionFailed("Expected symlink rejection")
        } catch ReconstructionArtifactClientError.unableToStoreDownload {
            // Expected.
        }
        let externalBytes = try Data(contentsOf: externalSentinel)
        try require(
            externalBytes == artifactBytes,
            "Expected external symlink target to remain untouched"
        )

        let cancelledTransport = StubReconstructionArtifactTransport(
            dataHandler: { _ in throw ArtifactVerificationError.unexpectedRequest },
            downloadHandler: { _ in
                try await Task.sleep(nanoseconds: 500_000_000)
                throw ArtifactVerificationError.unexpectedRequest
            }
        )
        let cancellationTask = Task {
            try await HTTPReconstructionArtifactClient(
                transport: cancelledTransport
            ).downloadArtifact(artifacts[0], scanID: "scan-1", baseURL: baseURL)
        }
        try await Task.sleep(nanoseconds: 25_000_000)
        cancellationTask.cancel()
        do {
            _ = try await cancellationTask.value
            throw ArtifactVerificationError.assertionFailed("Expected cancellation")
        } catch is CancellationError {
            // Expected.
        }

        let storeFile = fileManager.temporaryDirectory
            .appendingPathComponent("artifact-store-\(UUID().uuidString).ply")
        try artifactBytes.write(to: storeFile)
        let discardCapture = ArtifactDiscardCapture()
        let memoryClient = InMemoryReconstructionArtifactClient(
            listHandler: { scanID, _ in
                try require(scanID == "scan-1", "Expected store scan ID")
                return artifacts
            },
            downloadHandler: { artifact, scanID, _ in
                try require(scanID == "scan-1", "Expected store download scan ID")
                return DownloadedReconstructionArtifact(
                    artifact: artifact,
                    fileURL: storeFile
                )
            },
            discardHandler: { discarded in
                await discardCapture.record(discarded.fileURL)
                try? fileManager.removeItem(at: discarded.fileURL)
            }
        )
        let store = ReconstructionArtifactStore(client: memoryClient)
        await store.loadIfNeeded(scanID: "scan-1", baseURLString: "https://example.com")
        try require(store.artifacts == artifacts, "Expected store manifest")
        await store.download(
            artifacts[0],
            scanID: "scan-1",
            baseURLString: "https://example.com"
        )
        try require(store.sharedDownload?.fileURL == storeFile, "Expected share handoff")
        await store.clearSharedDownload()
        try require(store.sharedDownload == nil, "Expected share state cleanup")
        let discardedURLs = await discardCapture.snapshot()
        try require(
            discardedURLs == [storeFile],
            "Expected store-owned discard"
        )

        let lateFile = fileManager.temporaryDirectory
            .appendingPathComponent("artifact-late-\(UUID().uuidString).ply")
        try artifactBytes.write(to: lateFile)
        let lateDiscardCapture = ArtifactDiscardCapture()
        let lateClient = InMemoryReconstructionArtifactClient(
            listHandler: { _, _ in artifacts },
            downloadHandler: { artifact, _, _ in
                try await Task.sleep(nanoseconds: 50_000_000)
                return DownloadedReconstructionArtifact(
                    artifact: artifact,
                    fileURL: lateFile
                )
            },
            discardHandler: { discarded in
                await lateDiscardCapture.record(discarded.fileURL)
                try? fileManager.removeItem(at: discarded.fileURL)
            }
        )
        let lateStore = ReconstructionArtifactStore(client: lateClient)
        let lateDownloadTask = Task {
            await lateStore.download(
                artifacts[0],
                scanID: "scan-1",
                baseURLString: "https://example.com"
            )
        }
        try await Task.sleep(nanoseconds: 10_000_000)
        await lateStore.deactivate()
        await lateDownloadTask.value
        try require(
            lateStore.sharedDownload == nil,
            "Expected a result finishing after deactivation not to be shared"
        )
        let lateDiscardedURLs = await lateDiscardCapture.snapshot()
        try require(
            lateDiscardedURLs == [lateFile],
            "Expected a result finishing after deactivation to be discarded"
        )

        print("Verified reconstruction artifact client contract")
    }

    private static func require(
        _ condition: @autoclosure () -> Bool,
        _ message: String
    ) throws {
        if !condition() {
            throw ArtifactVerificationError.assertionFailed(message)
        }
    }
}
