import Foundation

struct StubScanUploadTransport: ScanUploadTransport {
    let handler: (URLRequest, URL) async throws -> (Data, URLResponse)

    func upload(for request: URLRequest, fromFile bodyURL: URL) async throws -> (Data, URLResponse) {
        try await handler(request, bodyURL)
    }
}

actor UploadBodyCapture {
    private(set) var bodyURL: URL?

    func record(_ url: URL) {
        bodyURL = url
    }
}

enum ScanUploadVerificationError: Error {
    case assertionFailed(String)
}

@main
struct VerifyScanUploadClient {
    @MainActor
    static func main() async throws {
        let archiveURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanner-upload-verifier-\(UUID().uuidString).zip")
        let archiveBytes = Data("zip-verifier-payload".utf8)
        try archiveBytes.write(to: archiveURL, options: .atomic)
        defer {
            try? FileManager.default.removeItem(at: archiveURL)
        }

        let capture = UploadBodyCapture()
        let successPayload = Self.jobPayload(
            scanID: "uploaded-job",
            status: "validated",
            message: "Scan package validated. Reconstruction was not requested."
        )
        let successTransport = StubScanUploadTransport { request, bodyURL in
            await capture.record(bodyURL)
            try require(request.httpMethod == "POST", "Expected POST upload")
            try require(request.url?.path == "/api/scans", "Expected nested scans endpoint")
            guard let requestURL = request.url else {
                throw ScanUploadVerificationError.assertionFailed("Expected upload URL")
            }
            let queryItems = URLComponents(
                url: requestURL,
                resolvingAgainstBaseURL: false
            )?.queryItems ?? []
            let query = Dictionary(uniqueKeysWithValues: queryItems.compactMap { item in
                item.value.map { (item.name, $0) }
            })
            try require(query["run_reconstruction"] == "true", "Expected reconstruction request")
            try require(query["run_dense"] == "true", "Expected dense reconstruction request")
            try require(query["run_openmvs"] == "true", "Expected OpenMVS request")
            try require(query["scope_mode"] == "auto_roi", "Expected automatic ROI scope request")
            try require(
                query["mask_profile"] == "scene_geometry",
                "Expected full-image scene alignment mask profile"
            )
            try require(query["review_scope"] == "true", "Expected sparse scope-review pause")

            let body = try Data(contentsOf: bodyURL)
            let contentType = request.value(forHTTPHeaderField: "Content-Type") ?? ""
            guard let boundary = contentType.components(separatedBy: "boundary=").last,
                  contentType.hasPrefix("multipart/form-data;") else {
                throw ScanUploadVerificationError.assertionFailed("Missing multipart boundary")
            }
            try require(
                request.value(forHTTPHeaderField: "Content-Length") == String(body.count),
                "Expected exact multipart content length"
            )
            try require(
                body.range(of: Data("name=\"file\"".utf8)) != nil,
                "Expected file form field"
            )
            try require(
                body.range(of: Data("filename=\"\(archiveURL.lastPathComponent)\"".utf8)) != nil,
                "Expected sanitized archive filename"
            )
            try require(
                body.range(of: archiveBytes) != nil,
                "Expected archive bytes in multipart body"
            )
            try require(
                body.suffix(Data("\r\n--\(boundary)--\r\n".utf8).count)
                    == Data("\r\n--\(boundary)--\r\n".utf8),
                "Expected closing multipart boundary"
            )

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            return (successPayload, response)
        }
        let abandonedBodyURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanner-upload-\(UUID().uuidString).multipart")
        try Data("abandoned-body".utf8).write(to: abandonedBodyURL)
        let client = HTTPScanUploadClient(transport: successTransport)
        try require(
            !FileManager.default.fileExists(atPath: abandonedBodyURL.path),
            "Expected abandoned multipart cleanup when the first client starts"
        )
        let job = try await client.uploadScan(
            archiveURL: archiveURL,
            baseURL: URL(string: "http://127.0.0.1:8000/api")!
        )
        try require(job.scanID == "uploaded-job", "Expected decoded uploaded job")
        if let bodyURL = await capture.bodyURL {
            try require(
                !FileManager.default.fileExists(atPath: bodyURL.path),
                "Expected multipart body cleanup after success"
            )
        } else {
            throw ScanUploadVerificationError.assertionFailed("Expected captured multipart body")
        }

        do {
            _ = try await client.uploadScan(
                archiveURL: archiveURL,
                baseURL: URL(string: "http://8.8.8.8:8000")!
            )
            throw ScanUploadVerificationError.assertionFailed("Expected public HTTP rejection")
        } catch ReconstructionJobClientError.insecureNonLocalURL {
            // Expected.
        }

        do {
            _ = try await client.uploadScan(
                archiveURL: archiveURL.deletingPathExtension().appendingPathExtension("mov"),
                baseURL: URL(string: "https://example.com")!
            )
            throw ScanUploadVerificationError.assertionFailed("Expected non-ZIP rejection")
        } catch ScanUploadClientError.archiveMustBeZip {
            // Expected.
        }

        let symlinkURL = archiveURL.deletingLastPathComponent()
            .appendingPathComponent("scanner-upload-link-\(UUID().uuidString).zip")
        try FileManager.default.createSymbolicLink(
            at: symlinkURL,
            withDestinationURL: archiveURL
        )
        defer {
            try? FileManager.default.removeItem(at: symlinkURL)
        }
        do {
            _ = try await client.uploadScan(
                archiveURL: symlinkURL,
                baseURL: URL(string: "https://example.com")!
            )
            throw ScanUploadVerificationError.assertionFailed("Expected archive symlink rejection")
        } catch ScanUploadClientError.archiveIsSymbolicLink {
            // Expected.
        }

        let failureTransport = StubScanUploadTransport { request, _ in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 413,
                httpVersion: nil,
                headerFields: nil
            )!
            return (Data(), response)
        }
        do {
            _ = try await HTTPScanUploadClient(transport: failureTransport).uploadScan(
                archiveURL: archiveURL,
                baseURL: URL(string: "https://example.com")!
            )
            throw ScanUploadVerificationError.assertionFailed("Expected upload HTTP failure")
        } catch ScanUploadClientError.httpStatus(413) {
            // Expected.
        }

        let delayedCapture = UploadBodyCapture()
        let delayedTransport = StubScanUploadTransport { _, bodyURL in
            await delayedCapture.record(bodyURL)
            try await Task.sleep(nanoseconds: 500_000_000)
            throw ScanUploadVerificationError.assertionFailed("Expected cancellation")
        }
        let cancelledTask = Task {
            try await HTTPScanUploadClient(transport: delayedTransport).uploadScan(
                archiveURL: archiveURL,
                baseURL: URL(string: "https://example.com")!
            )
        }
        try await Task.sleep(nanoseconds: 25_000_000)
        cancelledTask.cancel()
        do {
            _ = try await cancelledTask.value
            throw ScanUploadVerificationError.assertionFailed("Expected cancelled upload")
        } catch is CancellationError {
            // Expected.
        }
        if let bodyURL = await delayedCapture.bodyURL {
            try require(
                !FileManager.default.fileExists(atPath: bodyURL.path),
                "Expected multipart body cleanup after cancellation"
            )
        }

        let largeArchiveURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanner-upload-large-\(UUID().uuidString).zip")
        guard FileManager.default.createFile(atPath: largeArchiveURL.path, contents: nil) else {
            throw ScanUploadVerificationError.assertionFailed("Could not create large test archive")
        }
        let largeArchiveSize: UInt64 = 64 * 1024 * 1024
        let largeArchiveHandle = try FileHandle(forWritingTo: largeArchiveURL)
        try largeArchiveHandle.truncate(atOffset: largeArchiveSize)
        try largeArchiveHandle.close()
        defer {
            try? FileManager.default.removeItem(at: largeArchiveURL)
        }

        let existingBodies = try multipartBodyURLs()
        let unexpectedTransport = StubScanUploadTransport { _, _ in
            throw ScanUploadVerificationError.assertionFailed(
                "Cancelled multipart construction reached the transport"
            )
        }
        let constructionTask = Task {
            try await HTTPScanUploadClient(transport: unexpectedTransport).uploadScan(
                archiveURL: largeArchiveURL,
                baseURL: URL(string: "https://example.com")!
            )
        }
        var partialBodyURL: URL?
        for _ in 0..<500 {
            let newBodies = try multipartBodyURLs().subtracting(existingBodies)
            if let bodyURL = newBodies.first,
               let size = try? bodyURL.resourceValues(forKeys: [.fileSizeKey]).fileSize,
               size < Int(largeArchiveSize) {
                partialBodyURL = bodyURL
                break
            }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        guard let partialBodyURL else {
            constructionTask.cancel()
            _ = try? await constructionTask.value
            throw ScanUploadVerificationError.assertionFailed(
                "Expected to observe multipart construction in progress"
            )
        }
        constructionTask.cancel()
        do {
            _ = try await constructionTask.value
            throw ScanUploadVerificationError.assertionFailed(
                "Expected multipart construction cancellation"
            )
        } catch is CancellationError {
            // Expected.
        }
        try require(
            !FileManager.default.fileExists(atPath: partialBodyURL.path),
            "Expected partial multipart cleanup after construction cancellation"
        )

        let memoryClient = InMemoryScanUploadClient { _, _ in job }
        let store = ScanUploadStore(client: memoryClient)
        await store.upload(
            archiveURL: archiveURL,
            baseURLString: "https://example.com"
        )
        try require(!store.isUploading, "Expected upload state to clear")
        try require(store.notice?.kind == .success, "Expected success notice")
        try require(
            store.notice?.message.contains("Check the Jobs tab") == true,
            "Expected processing-history handoff"
        )

        let failedJob = try JSONDecoder().decode(
            ReconstructionJob.self,
            from: Self.jobPayload(
                scanID: "failed-job",
                status: "failed",
                message: "Frame metadata count did not match."
            )
        )
        let rejectedStore = ScanUploadStore(
            client: InMemoryScanUploadClient { _, _ in failedJob }
        )
        await rejectedStore.upload(
            archiveURL: archiveURL,
            baseURLString: "https://example.com"
        )
        try require(rejectedStore.notice?.kind == .failure, "Expected rejected notice")
        try require(
            rejectedStore.notice?.message.contains("Frame metadata") == true,
            "Expected backend validation detail"
        )

        print("Verified scan upload client contract")
    }

    private static func jobPayload(scanID: String, status: String, message: String) -> Data {
        Data(
            """
            {
              "scan_id": "\(scanID)",
              "status": "\(status)",
              "stage": "finished",
              "message": "\(message)",
              "image_count": 12,
              "frame_count": 12,
              "outputs": {},
              "created_at": "2026-07-10T10:00:00+00:00",
              "updated_at": "2026-07-10T10:01:00+00:00",
              "finished_at": "2026-07-10T10:01:00+00:00"
            }
            """.utf8
        )
    }

    private static func require(
        _ condition: @autoclosure () -> Bool,
        _ message: String
    ) throws {
        if !condition() {
            throw ScanUploadVerificationError.assertionFailed(message)
        }
    }

    private static func multipartBodyURLs() throws -> Set<URL> {
        let temporaryDirectory = FileManager.default.temporaryDirectory
        return Set(
            try FileManager.default.contentsOfDirectory(
                at: temporaryDirectory,
                includingPropertiesForKeys: nil
            ).filter { $0.lastPathComponent.hasPrefix("scanner-upload-") }
        )
    }
}
