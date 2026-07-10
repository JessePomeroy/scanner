import Combine
import Foundation

@MainActor
final class ReconstructionJobStore: ObservableObject {
    @Published private(set) var jobs: [ReconstructionJob] = []
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var hasLoaded = false

    private let client: any ReconstructionJobLoading
    private var refreshSequence = 0
    private var historySource: String?

    init(client: any ReconstructionJobLoading) {
        self.client = client
    }

    func backendURLDidChange(to baseURLString: String) {
        let source = normalized(baseURLString)
        guard historySource != source else { return }

        refreshSequence += 1
        historySource = source
        jobs = []
        isLoading = false
        errorMessage = nil
        hasLoaded = false
    }

    func loadIfNeeded(baseURLString: String) async {
        backendURLDidChange(to: baseURLString)
        guard !hasLoaded, !isLoading else { return }
        await refresh(baseURLString: baseURLString)
    }

    func refresh(baseURLString: String) async {
        backendURLDidChange(to: baseURLString)
        refreshSequence += 1
        let sequence = refreshSequence
        isLoading = true
        errorMessage = nil

        let trimmedURL = normalized(baseURLString)
        guard let baseURL = URL(string: trimmedURL), !trimmedURL.isEmpty else {
            finish(sequence: sequence, error: ReconstructionJobClientError.invalidBaseURL)
            return
        }

        do {
            let loadedJobs = try await client.listJobs(baseURL: baseURL, limit: 50)
            guard !Task.isCancelled else {
                cancel(sequence: sequence)
                return
            }
            guard sequence == refreshSequence else { return }
            jobs = loadedJobs
            hasLoaded = true
            isLoading = false
        } catch is CancellationError {
            cancel(sequence: sequence)
        } catch let error as URLError where error.code == .cancelled {
            cancel(sequence: sequence)
        } catch {
            finish(sequence: sequence, error: error)
        }
    }

    private func normalized(_ baseURLString: String) -> String {
        baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func cancel(sequence: Int) {
        guard sequence == refreshSequence else { return }
        isLoading = false
    }

    private func finish(sequence: Int, error: Error) {
        guard sequence == refreshSequence else { return }
        hasLoaded = true
        isLoading = false
        errorMessage = error.localizedDescription
    }
}
