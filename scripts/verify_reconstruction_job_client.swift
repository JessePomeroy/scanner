import Foundation

final class MockJobURLProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: VerificationError.missingHandler)
            return
        }

        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

enum VerificationError: Error {
    case missingHandler
    case assertionFailed(String)
}

struct DelayedSourceJobClient: ReconstructionJobLoading {
    let delayedHost: String
    let delayedJobs: [ReconstructionJob]
    let immediateJobs: [ReconstructionJob]

    func listJobs(baseURL: URL, limit: Int) async throws -> [ReconstructionJob] {
        if baseURL.host == delayedHost {
            try await Task.sleep(nanoseconds: 150_000_000)
            return Array(delayedJobs.prefix(limit))
        }
        return Array(immediateJobs.prefix(limit))
    }
}

@main
struct VerifyReconstructionJobClient {
    @MainActor
    static func main() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockJobURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = HTTPReconstructionJobClient(session: session)
        let baseURL = URL(string: "http://127.0.0.1:8000/api")!

        MockJobURLProtocol.handler = { request in
            guard request.url?.path == "/api/scans",
                  URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                    .queryItems?.first(where: { $0.name == "limit" })?.value == "2" else {
                throw VerificationError.assertionFailed("Unexpected list-jobs request URL")
            }
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            let payload = Data(
                """
                [
                  {
                    "scan_id": "scan-complete",
                    "status": "complete",
                    "stage": "finished",
                    "message": "Reconstruction completed.",
                    "image_count": 42,
                    "frame_count": 42,
                    "outputs": {"package_dir": "/tmp/complete"},
                    "created_at": "2026-07-10T02:00:00+00:00",
                    "updated_at": "2026-07-10T02:05:00.123456+00:00",
                    "started_at": "2026-07-10T02:01:00+00:00",
                    "finished_at": "2026-07-10T02:05:00.123456+00:00"
                  },
                  {
                    "scan_id": "scan-future",
                    "status": "paused",
                    "stage": "awaiting_gpu",
                    "outputs": {}
                  }
                ]
                """.utf8
            )
            return (response, payload)
        }

        let jobs = try await client.listJobs(baseURL: baseURL, limit: 2)
        try require(jobs.count == 2, "Expected two decoded jobs")
        try require(jobs[0].status == .complete, "Expected complete status")
        try require(jobs[0].updatedAt != nil, "Expected fractional timestamp decoding")
        guard case .unknown("paused") = jobs[1].status,
              case .unknown("awaiting_gpu") = jobs[1].stage else {
            throw VerificationError.assertionFailed("Expected forward-compatible status and stage")
        }

        let memoryClient = InMemoryReconstructionJobClient(jobs: jobs)
        let limitedJobs = try await memoryClient.listJobs(baseURL: baseURL, limit: 1)
        try require(limitedJobs.map(\.scanID) == ["scan-complete"], "Expected in-memory limit")

        let publicHTTPSJobs = try await client.listJobs(
            baseURL: URL(string: "https://example.com/api")!,
            limit: 2
        )
        try require(publicHTTPSJobs.count == 2, "Expected public HTTPS to remain supported")

        do {
            _ = try await client.listJobs(baseURL: URL(string: "http://8.8.8.8:8000")!, limit: 2)
            throw VerificationError.assertionFailed("Expected public cleartext URL failure")
        } catch ReconstructionJobClientError.insecureNonLocalURL {
            // Expected.
        }
        do {
            _ = try await client.listJobs(baseURL: URL(string: "http://scanner.example")!, limit: 2)
            throw VerificationError.assertionFailed("Expected public hostname cleartext failure")
        } catch ReconstructionJobClientError.insecureNonLocalURL {
            // Expected.
        }

        let sourceClient = DelayedSourceJobClient(
            delayedHost: "backend-a.example",
            delayedJobs: [jobs[0]],
            immediateJobs: [jobs[1]]
        )
        let store = ReconstructionJobStore(client: sourceClient)
        let backendA = "https://backend-a.example"
        let backendB = "https://backend-b.example"
        let firstRefresh = Task {
            await store.refresh(baseURLString: backendA)
        }
        try await Task.sleep(nanoseconds: 25_000_000)
        store.backendURLDidChange(to: backendB)
        try require(store.jobs.isEmpty, "Expected source edit to clear prior rows")
        try require(!store.hasLoaded, "Expected source edit to require a new load")
        await firstRefresh.value
        try require(store.jobs.isEmpty, "Expected stale in-flight response to be discarded")

        await store.loadIfNeeded(baseURLString: backendB)
        try require(
            store.jobs.map(\.scanID) == ["scan-future"],
            "Expected re-entry load to use the current backend"
        )

        MockJobURLProtocol.handler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 503,
                httpVersion: nil,
                headerFields: nil
            )!
            return (response, Data())
        }
        do {
            _ = try await client.listJobs(baseURL: baseURL, limit: 2)
            throw VerificationError.assertionFailed("Expected HTTP failure")
        } catch ReconstructionJobClientError.httpStatus(503) {
            // Expected.
        }

        do {
            _ = try await client.listJobs(baseURL: URL(fileURLWithPath: "/tmp"), limit: 2)
            throw VerificationError.assertionFailed("Expected invalid URL failure")
        } catch ReconstructionJobClientError.invalidBaseURL {
            // Expected.
        }

        print("Verified reconstruction job client contract")
    }

    private static func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        if !condition() {
            throw VerificationError.assertionFailed(message)
        }
    }
}
