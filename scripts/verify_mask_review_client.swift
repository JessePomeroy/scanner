import Foundation

private enum VerificationError: Error {
    case failed(String)
}

private final class RequestCapture: @unchecked Sendable {
    var requests: [URLRequest] = []
}

private struct StubTransport: MaskReviewTransport {
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

private let validReport = Data(
    """
    {
      "schema_version": "1.0",
      "state": "awaiting_review",
      "generator": "polygon_keyframe_interpolation_v1",
      "source_authoring_revision": 2,
      "frame_count": 9,
      "review_indices": [0, 2, 4, 6, 8],
      "review_masks": [
        "masks/review/frame_0.jpg.png",
        "masks/review/frame_2.jpg.png",
        "masks/review/frame_4.jpg.png",
        "masks/review/frame_6.jpg.png",
        "masks/review/frame_8.jpg.png"
      ],
      "quality": {
        "blocking_issue_count": 0,
        "warning_count": 1,
        "blocking_issues": [],
        "warnings": [{
          "code": "low_generator_confidence",
          "frame_id": 8,
          "message": "The last frame used a low-confidence boundary hold."
        }]
      },
      "frames": []
    }
    """.utf8
)

@main
private enum MaskReviewClientVerifier {
    static func main() async throws {
        let scanID = "scan-123"
        let baseURL = URL(string: "https://scanner.example/api")!
        let loadCapture = RequestCapture()
        let loadClient = HTTPMaskReviewClient(
            transport: StubTransport { request in
                loadCapture.requests.append(request)
                return (validReport, response(url: request.url!, status: 200))
            }
        )

        let report = try await loadClient.loadReview(scanID: scanID, baseURL: baseURL)
        try require(report.isValid, "Valid review evidence was rejected")
        try require(report.reviewMasks.count == 5, "Expected five review samples")
        try require(report.quality.warningCount == 1, "Warning count changed")
        try require(loadCapture.requests[0].httpMethod == "GET", "Review load did not use GET")
        try require(
            loadCapture.requests[0].url?.absoluteString
                == "https://scanner.example/api/scans/scan-123/mask-review",
            "Review URL was incorrect"
        )

        let approvalCapture = RequestCapture()
        let approvalClient = HTTPMaskReviewClient(
            transport: StubTransport { request in
                approvalCapture.requests.append(request)
                let job = Data(
                    """
                    {"scan_id":"scan-123","status":"processing","stage":"awaiting_scope","outputs":{}}
                    """.utf8
                )
                return (job, response(url: request.url!, status: 200))
            }
        )
        let approved = try await approvalClient.approve(scanID: scanID, baseURL: baseURL)
        try require(approved.scanID == scanID, "Approval decoded the wrong job")
        try require(approvalCapture.requests[0].httpMethod == "POST", "Approval did not use POST")
        try require(
            approvalCapture.requests[0].url?.absoluteString
                == "https://scanner.example/api/scans/scan-123/mask-review/approve",
            "Approval URL was incorrect"
        )
        let rejected = try await approvalClient.reject(scanID: scanID, baseURL: baseURL)
        try require(rejected.scanID == scanID, "Rejection decoded the wrong job")
        try require(approvalCapture.requests[1].httpMethod == "POST", "Rejection did not use POST")
        try require(
            approvalCapture.requests[1].url?.absoluteString
                == "https://scanner.example/api/scans/scan-123/mask-review/reject",
            "Rejection URL was incorrect"
        )

        let invalidReport = Data(
            String(decoding: validReport, as: UTF8.self)
                .replacingOccurrences(of: "\"warning_count\": 1", with: "\"warning_count\": 2")
                .utf8
        )
        let invalidClient = HTTPMaskReviewClient(
            transport: StubTransport { request in
                (invalidReport, response(url: request.url!, status: 200))
            }
        )
        do {
            _ = try await invalidClient.loadReview(scanID: scanID, baseURL: baseURL)
            throw VerificationError.failed("Invalid quality counts were accepted")
        } catch MaskReviewClientError.invalidPayload {
            // Expected.
        }

        do {
            _ = try await loadClient.loadReview(scanID: "../unsafe", baseURL: baseURL)
            throw VerificationError.failed("Unsafe scan ID was accepted")
        } catch MaskReviewClientError.invalidScanID {
            // Expected.
        }

        print("Mask review client verification passed")
    }
}
