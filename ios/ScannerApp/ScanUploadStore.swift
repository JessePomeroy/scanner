import Combine
import Foundation

struct ScanUploadNotice: Identifiable {
    enum Kind: Equatable {
        case success
        case failure
    }

    let id = UUID()
    let kind: Kind
    let title: String
    let message: String
    /// Where to continue in the browser, when the upload needs one more step.
    var pageURL: URL? = nil
}

@MainActor
final class ScanUploadStore: ObservableObject {
    @Published private(set) var uploadingArchiveURL: URL?
    @Published private(set) var notice: ScanUploadNotice?

    private let client: ScanUploading

    init(client: ScanUploading) {
        self.client = client
    }

    var isUploading: Bool {
        uploadingArchiveURL != nil
    }

    func isUploading(_ archiveURL: URL) -> Bool {
        uploadingArchiveURL == archiveURL
    }

    func upload(
        archiveURL: URL,
        baseURLString: String,
        flow: ScanUploadFlow = .review(.sceneGeometry)
    ) async {
        guard uploadingArchiveURL == nil else { return }

        let trimmedBaseURL = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let baseURL = URL(string: trimmedBaseURL), !trimmedBaseURL.isEmpty else {
            notice = ScanUploadNotice(
                kind: .failure,
                title: "Unable to Upload Scan",
                message: ReconstructionJobClientError.invalidBaseURL.localizedDescription
            )
            return
        }

        uploadingArchiveURL = archiveURL
        notice = nil
        defer {
            uploadingArchiveURL = nil
        }

        do {
            let job = try await client.uploadScan(
                archiveURL: archiveURL,
                baseURL: baseURL,
                flow: flow
            )
            notice = Self.notice(for: job, archiveURL: archiveURL, flow: flow, baseURL: baseURL)
        } catch is CancellationError {
            notice = ScanUploadNotice(
                kind: .failure,
                title: "Upload Cancelled",
                message: "The scan ZIP remains available in the gallery."
            )
        } catch {
            notice = ScanUploadNotice(
                kind: .failure,
                title: "Unable to Upload Scan",
                message: error.localizedDescription
            )
        }
    }

    func clearNotice() {
        notice = nil
    }

    private static func notice(
        for job: ReconstructionJob,
        archiveURL: URL,
        flow: ScanUploadFlow,
        baseURL: URL
    ) -> ScanUploadNotice {
        let scanName = archiveURL.deletingPathExtension().lastPathComponent
        let detail = job.message.flatMap { $0.isEmpty ? nil : $0 } ?? job.status.title

        if job.status == .failed {
            return ScanUploadNotice(
                kind: .failure,
                title: "Backend Rejected Scan",
                message: "\(scanName): \(detail)"
            )
        }
        if flow == .objectBox {
            return ScanUploadNotice(
                kind: .success,
                title: "Scan Uploaded",
                message: "\(scanName): draw one box around the object to start the model.",
                pageURL: ReconstructionBackendEndpoint.pageURL(baseURL: baseURL, scanID: job.scanID)
            )
        }
        return ScanUploadNotice(
            kind: .success,
            title: "Scan Uploaded",
            message: "\(scanName): \(detail) Check the Jobs tab for status."
        )
    }
}
