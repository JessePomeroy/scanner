import Combine
import Foundation

enum ReconstructionArtifactDownloadDestination: Equatable, Sendable {
    case share
    case pointCloudPreview
    case scopeEditor
}

@MainActor
final class ReconstructionArtifactStore: ObservableObject {
    @Published private(set) var artifacts: [ReconstructionArtifact] = []
    @Published private(set) var isLoading = false
    @Published private(set) var hasLoaded = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var downloadingArtifactID: ReconstructionArtifact.ID?
    @Published private(set) var sharedDownload: DownloadedReconstructionArtifact?
    @Published private(set) var previewedDownload: DownloadedReconstructionArtifact?
    @Published private(set) var scopeEditorDownload: DownloadedReconstructionArtifact?

    private let client: any ReconstructionArtifactAccessing
    private var refreshSequence = 0
    private var downloadSequence = 0

    init(client: any ReconstructionArtifactAccessing) {
        self.client = client
    }

    func loadIfNeeded(scanID: String, baseURLString: String) async {
        guard !hasLoaded, !isLoading else { return }
        await refresh(scanID: scanID, baseURLString: baseURLString)
    }

    func refresh(scanID: String, baseURLString: String) async {
        refreshSequence += 1
        let sequence = refreshSequence
        isLoading = true
        errorMessage = nil

        guard let baseURL = Self.baseURL(from: baseURLString) else {
            finish(sequence: sequence, error: ReconstructionJobClientError.invalidBaseURL)
            return
        }

        do {
            let loadedArtifacts = try await client.listArtifacts(
                scanID: scanID,
                baseURL: baseURL
            )
            guard !Task.isCancelled else {
                cancel(sequence: sequence)
                return
            }
            guard sequence == refreshSequence else { return }
            artifacts = loadedArtifacts
            isLoading = false
            hasLoaded = true
        } catch is CancellationError {
            cancel(sequence: sequence)
        } catch let error as URLError where error.code == .cancelled {
            cancel(sequence: sequence)
        } catch {
            finish(sequence: sequence, error: error)
        }
    }

    func download(
        _ artifact: ReconstructionArtifact,
        scanID: String,
        baseURLString: String,
        destination: ReconstructionArtifactDownloadDestination = .share
    ) async {
        guard downloadingArtifactID == nil else { return }
        guard destination == .share || artifact.supportsPointCloudPreview else {
            errorMessage = "Only PLY point clouds can be previewed."
            return
        }
        guard let baseURL = Self.baseURL(from: baseURLString) else {
            errorMessage = ReconstructionJobClientError.invalidBaseURL.localizedDescription
            return
        }

        downloadSequence += 1
        let sequence = downloadSequence
        downloadingArtifactID = artifact.id
        errorMessage = nil
        defer {
            if sequence == downloadSequence {
                downloadingArtifactID = nil
            }
        }

        do {
            let download = try await client.downloadArtifact(
                artifact,
                scanID: scanID,
                baseURL: baseURL
            )
            guard !Task.isCancelled, sequence == downloadSequence else {
                await client.discardDownloadedArtifact(download)
                return
            }
            let previousDownloads = releasePresentedDownloads()
            for previous in previousDownloads {
                await client.discardDownloadedArtifact(previous)
            }
            guard !Task.isCancelled, sequence == downloadSequence else {
                await client.discardDownloadedArtifact(download)
                return
            }
            switch destination {
            case .share:
                sharedDownload = download
            case .pointCloudPreview:
                previewedDownload = download
            case .scopeEditor:
                scopeEditorDownload = download
            }
        } catch is CancellationError {
            guard sequence == downloadSequence else { return }
            errorMessage = "Result download was cancelled."
        } catch let error as URLError where error.code == .cancelled {
            guard sequence == downloadSequence else { return }
            errorMessage = "Result download was cancelled."
        } catch {
            guard sequence == downloadSequence else { return }
            errorMessage = error.localizedDescription
        }
    }

    func clearSharedDownload() async {
        guard let download = sharedDownload else { return }
        sharedDownload = nil
        await client.discardDownloadedArtifact(download)
    }

    func dismissSharedDownload() {
        guard let download = sharedDownload else { return }
        sharedDownload = nil
        Task {
            await client.discardDownloadedArtifact(download)
        }
    }

    func clearPreviewedDownload() async {
        guard let download = previewedDownload else { return }
        previewedDownload = nil
        await client.discardDownloadedArtifact(download)
    }

    func dismissPreviewedDownload() {
        guard let download = previewedDownload else { return }
        previewedDownload = nil
        Task {
            await client.discardDownloadedArtifact(download)
        }
    }

    func dismissScopeEditorDownload() {
        guard let download = scopeEditorDownload else { return }
        scopeEditorDownload = nil
        Task {
            await client.discardDownloadedArtifact(download)
        }
    }

    func deactivate() async {
        downloadSequence += 1
        downloadingArtifactID = nil
        errorMessage = nil
        let downloads = releasePresentedDownloads()
        for download in downloads {
            await client.discardDownloadedArtifact(download)
        }
    }

    func clearError() {
        errorMessage = nil
    }

    func isDownloading(_ artifact: ReconstructionArtifact) -> Bool {
        downloadingArtifactID == artifact.id
    }

    private static func baseURL(from value: String) -> URL? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return URL(string: trimmed)
    }

    private func cancel(sequence: Int) {
        guard sequence == refreshSequence else { return }
        isLoading = false
    }

    private func finish(sequence: Int, error: Error) {
        guard sequence == refreshSequence else { return }
        isLoading = false
        hasLoaded = true
        errorMessage = error.localizedDescription
    }

    private func releasePresentedDownloads() -> [DownloadedReconstructionArtifact] {
        let downloads = [sharedDownload, previewedDownload, scopeEditorDownload].compactMap { $0 }
        sharedDownload = nil
        previewedDownload = nil
        scopeEditorDownload = nil
        return downloads
    }
}
