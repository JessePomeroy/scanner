import Foundation

protocol ScanUploading {
    func uploadScan(archiveURL: URL, baseURL: URL) async throws -> ReconstructionJob
}

protocol ScanUploadTransport {
    func upload(for request: URLRequest, fromFile bodyURL: URL) async throws -> (Data, URLResponse)
}

enum ScanUploadClientError: LocalizedError, Equatable {
    case archiveNotFound
    case archiveMustBeZip
    case archiveIsSymbolicLink
    case multipartBodyCreationFailed
    case invalidResponse
    case httpStatus(Int)
    case invalidPayload

    var errorDescription: String? {
        switch self {
        case .archiveNotFound:
            return "The exported scan ZIP could not be found."
        case .archiveMustBeZip:
            return "Only exported ZIP scan packages can be uploaded."
        case .archiveIsSymbolicLink:
            return "The scan ZIP must be stored directly in the app's gallery."
        case .multipartBodyCreationFailed:
            return "The scan ZIP could not be prepared for upload."
        case .invalidResponse:
            return "The backend returned an invalid upload response."
        case .httpStatus(let status):
            return "The backend returned HTTP status \(status) during upload."
        case .invalidPayload:
            return "The uploaded job response could not be decoded."
        }
    }
}

struct URLSessionScanUploadTransport: ScanUploadTransport {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func upload(for request: URLRequest, fromFile bodyURL: URL) async throws -> (Data, URLResponse) {
        try await session.upload(for: request, fromFile: bodyURL)
    }
}

struct HTTPScanUploadClient: ScanUploading {
    private let transport: ScanUploadTransport

    init(transport: ScanUploadTransport = URLSessionScanUploadTransport()) {
        self.transport = transport
    }

    func uploadScan(archiveURL: URL, baseURL: URL) async throws -> ReconstructionJob {
        try Self.validateArchive(archiveURL)
        let endpoint = try ReconstructionBackendEndpoint.scansURL(baseURL: baseURL)
        let boundary = "ScannerBoundary-\(UUID().uuidString)"

        let multipart: MultipartFormUpload
        do {
            try Task.checkCancellation()
            let buildTask = Task.detached(priority: .userInitiated) {
                try MultipartFormUpload.create(
                    archiveURL: archiveURL,
                    boundary: boundary
                )
            }
            let builtMultipart = try await withTaskCancellationHandler {
                try await buildTask.value
            } onCancel: {
                buildTask.cancel()
            }
            if Task.isCancelled {
                try? FileManager.default.removeItem(at: builtMultipart.bodyURL)
                throw CancellationError()
            }
            multipart = builtMultipart
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as ScanUploadClientError {
            throw error
        } catch {
            throw ScanUploadClientError.multipartBodyCreationFailed
        }
        defer {
            try? FileManager.default.removeItem(at: multipart.bodyURL)
        }

        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 10 * 60
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(
            "multipart/form-data; boundary=\(boundary)",
            forHTTPHeaderField: "Content-Type"
        )
        request.setValue(String(multipart.byteCount), forHTTPHeaderField: "Content-Length")

        let (data, response) = try await transport.upload(for: request, fromFile: multipart.bodyURL)
        guard let response = response as? HTTPURLResponse else {
            throw ScanUploadClientError.invalidResponse
        }
        guard (200...299).contains(response.statusCode) else {
            throw ScanUploadClientError.httpStatus(response.statusCode)
        }

        do {
            return try JSONDecoder().decode(ReconstructionJob.self, from: data)
        } catch {
            throw ScanUploadClientError.invalidPayload
        }
    }

    private static func validateArchive(_ archiveURL: URL) throws {
        guard archiveURL.isFileURL,
              archiveURL.pathExtension.lowercased() == "zip" else {
            throw ScanUploadClientError.archiveMustBeZip
        }

        let values: URLResourceValues
        do {
            values = try archiveURL.resourceValues(
                forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
            )
        } catch {
            throw ScanUploadClientError.archiveNotFound
        }
        guard values.isSymbolicLink != true else {
            throw ScanUploadClientError.archiveIsSymbolicLink
        }
        guard values.isRegularFile == true else {
            throw ScanUploadClientError.archiveNotFound
        }
    }
}

struct InMemoryScanUploadClient: ScanUploading {
    let handler: (URL, URL) async throws -> ReconstructionJob

    func uploadScan(archiveURL: URL, baseURL: URL) async throws -> ReconstructionJob {
        try await handler(archiveURL, baseURL)
    }
}

private struct MultipartFormUpload: Sendable {
    let bodyURL: URL
    let byteCount: Int64

    static func create(archiveURL: URL, boundary: String) throws -> MultipartFormUpload {
        let fileManager = FileManager.default
        let bodyURL = fileManager.temporaryDirectory
            .appendingPathComponent("scanner-upload-\(UUID().uuidString).multipart")
        guard fileManager.createFile(atPath: bodyURL.path, contents: nil) else {
            throw ScanUploadClientError.multipartBodyCreationFailed
        }

        do {
            try Task.checkCancellation()
            let output = try FileHandle(forWritingTo: bodyURL)
            defer {
                try? output.close()
            }
            let input = try FileHandle(forReadingFrom: archiveURL)
            defer {
                try? input.close()
            }

            let filename = sanitizedFilename(archiveURL.lastPathComponent)
            let header = "--\(boundary)\r\n"
                + "Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n"
                + "Content-Type: application/zip\r\n"
                + "\r\n"
            try output.write(contentsOf: Data(header.utf8))

            while let chunk = try input.read(upToCount: 1024 * 1024), !chunk.isEmpty {
                try Task.checkCancellation()
                try output.write(contentsOf: chunk)
            }

            let footer = "\r\n--\(boundary)--\r\n"
            try output.write(contentsOf: Data(footer.utf8))
            try output.synchronize()
            let byteCount = try output.offset()
            return MultipartFormUpload(bodyURL: bodyURL, byteCount: Int64(byteCount))
        } catch is CancellationError {
            try? fileManager.removeItem(at: bodyURL)
            throw CancellationError()
        } catch {
            try? fileManager.removeItem(at: bodyURL)
            if let error = error as? ScanUploadClientError {
                throw error
            }
            throw ScanUploadClientError.multipartBodyCreationFailed
        }
    }

    private static func sanitizedFilename(_ filename: String) -> String {
        let invalid = CharacterSet(charactersIn: "\\\"\r\n")
        let parts = filename.components(separatedBy: invalid)
        let sanitized = parts.joined(separator: "_")
        return sanitized.isEmpty ? "scan.zip" : sanitized
    }
}
