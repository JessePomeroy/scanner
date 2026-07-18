import Foundation

private enum VerificationError: Error {
    case failed(String)
}

private final class RequestCapture: @unchecked Sendable {
    var requests: [URLRequest] = []
}

private struct StubTransport: ReconstructionScopeTransport {
    let handler: @Sendable (URLRequest) async throws -> (Data, URLResponse)

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await handler(request)
    }
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    guard condition() else { throw VerificationError.failed(message) }
}

private func response(url: URL, status: Int) -> HTTPURLResponse {
    HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!
}

private func region(revision: Int = 1) throws -> ReconstructionRegion {
    try ReconstructionRegion.userRegion(
        center: [1, 2, 3],
        extents: [4, 5, 6],
        eulerRadians: [0, 0, .pi / 2],
        revision: revision
    )
}

private func envelope(scanID: String, region: ReconstructionRegion) throws -> Data {
    let encoded = try JSONEncoder().encode(region)
    let object = try JSONSerialization.jsonObject(with: encoded)
    return try JSONSerialization.data(withJSONObject: ["scan_id": scanID, "region": object])
}

@main
private enum ReconstructionScopeClientVerifier {
    static func main() async throws {
        let scanID = "scan-123"
        let baseURL = URL(string: "https://scanner.example/api")!
        let expected = try region()

        let loadCapture = RequestCapture()
        let loadClient = HTTPReconstructionScopeClient(
            transport: StubTransport { request in
                loadCapture.requests.append(request)
                return (
                    try envelope(scanID: scanID, region: expected),
                    response(url: request.url!, status: 200)
                )
            }
        )
        let loaded = try await loadClient.loadRegion(scanID: scanID, baseURL: baseURL)
        try require(loaded == expected, "GET did not decode the saved region")
        try require(loadCapture.requests.count == 1, "GET request count was incorrect")
        try require(loadCapture.requests[0].httpMethod == "GET", "Scope load did not use GET")
        try require(
            loadCapture.requests[0].url?.absoluteString == "https://scanner.example/api/scans/scan-123/scope",
            "Scope URL was incorrect"
        )

        let missingClient = HTTPReconstructionScopeClient(
            transport: StubTransport { request in
                (Data(), response(url: request.url!, status: 404))
            }
        )
        let missing = try await missingClient.loadRegion(scanID: scanID, baseURL: baseURL)
        try require(missing == nil, "HTTP 404 should mean no scope has been saved")

        let saveCapture = RequestCapture()
        let revised = try region(revision: 2)
        let saveClient = HTTPReconstructionScopeClient(
            transport: StubTransport { request in
                saveCapture.requests.append(request)
                return (
                    try envelope(scanID: scanID, region: revised),
                    response(url: request.url!, status: 200)
                )
            }
        )
        let saved = try await saveClient.saveRegion(revised, scanID: scanID, baseURL: baseURL)
        try require(saved == revised, "PUT did not decode the saved revision")
        try require(saveCapture.requests[0].httpMethod == "PUT", "Scope save did not use PUT")
        try require(
            saveCapture.requests[0].value(forHTTPHeaderField: "Content-Type") == "application/json",
            "Scope save did not declare JSON"
        )
        let sent = try JSONDecoder().decode(
            ReconstructionRegion.self,
            from: saveCapture.requests[0].httpBody ?? Data()
        )
        try require(sent == revised, "PUT body did not preserve the region contract")
        try require(abs(revised.orientationXYZW[2] - sqrt(0.5)) < 0.000_001, "Euler conversion was incorrect")
        try require(abs(revised.orientationXYZW[3] - sqrt(0.5)) < 0.000_001, "Quaternion was not normalized")
        try require(
            revised.contains(x: 1, y: 3.9, z: 3),
            "Oriented-box inclusion did not account for rotation"
        )

        let resumeCapture = RequestCapture()
        let resumeClient = HTTPReconstructionScopeClient(
            transport: StubTransport { request in
                resumeCapture.requests.append(request)
                let data = Data(
                    """
                    {"scan_id":"scan-123","status":"processing","stage":"reconstructing","outputs":{}}
                    """.utf8
                )
                return (data, response(url: request.url!, status: 200))
            }
        )
        let resumed = try await resumeClient.resume(scanID: scanID, baseURL: baseURL)
        try require(resumed.scanID == scanID, "Resume decoded the wrong job")
        try require(resumeCapture.requests[0].httpMethod == "POST", "Resume did not use POST")
        try require(
            resumeCapture.requests[0].url?.absoluteString == "https://scanner.example/api/scans/scan-123/resume",
            "Resume URL was incorrect"
        )
        try require(
            !revised.contains(x: 4.1, y: 2, z: 3),
            "Oriented-box inclusion accepted an excluded point"
        )

        let cameraPayload = Data(
            """
            {
              "schema_version": "1.0",
              "coordinate_system": "colmap_reconstruction",
              "camera_count": 1,
              "cameras": [{
                "image_id": 1,
                "image_name": "frame_000001.jpg",
                "camera_id": 1,
                "rotation_world_to_camera_wxyz": [1, 0, 0, 0],
                "translation_world_to_camera": [0, 0, 0],
                "center": [1, 2, 3]
              }]
            }
            """.utf8
        )
        let cameraURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanner-camera-preview-\(UUID().uuidString).json")
        try cameraPayload.write(to: cameraURL)
        defer { try? FileManager.default.removeItem(at: cameraURL) }
        let cameraPreview = try SparseCameraPreview.load(fileURL: cameraURL)
        try require(cameraPreview.cameraCount == 1, "Camera preview was not decoded")
        try require(cameraPreview.cameras[0].center == [1, 2, 3], "Camera center changed")

        let staleClient = HTTPReconstructionScopeClient(
            transport: StubTransport { request in
                (Data(), response(url: request.url!, status: 409))
            }
        )
        do {
            _ = try await staleClient.saveRegion(revised, scanID: scanID, baseURL: baseURL)
            throw VerificationError.failed("HTTP 409 was not surfaced as a stale revision")
        } catch ReconstructionScopeClientError.staleRevision {
            // Expected.
        }

        do {
            _ = try await loadClient.loadRegion(scanID: "../unsafe", baseURL: baseURL)
            throw VerificationError.failed("Unsafe scan id was accepted")
        } catch ReconstructionScopeClientError.invalidScanID {
            // Expected.
        }

        print("Reconstruction scope client verification passed")
    }
}
