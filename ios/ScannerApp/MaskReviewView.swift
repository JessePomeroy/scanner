import SwiftUI
import UIKit

@MainActor
final class MaskReviewStore: ObservableObject {
    @Published private(set) var report: MaskReviewReport?
    @Published private(set) var samples: [DownloadedReconstructionArtifact] = []
    @Published private(set) var isLoading = false
    @Published private(set) var isDeciding = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var decisionMessage: String?
    @Published private(set) var approved = false
    @Published private(set) var rejected = false

    private let reviewClient: any MaskReviewAccessing
    private let artifactClient: any ReconstructionArtifactAccessing
    private var loadSequence = 0

    init(
        reviewClient: any MaskReviewAccessing,
        artifactClient: any ReconstructionArtifactAccessing
    ) {
        self.reviewClient = reviewClient
        self.artifactClient = artifactClient
    }

    func load(
        scanID: String,
        baseURLString: String,
        artifacts: [ReconstructionArtifact]
    ) async {
        guard report == nil, !isLoading else { return }
        guard let baseURL = Self.baseURL(from: baseURLString) else {
            errorMessage = ReconstructionJobClientError.invalidBaseURL.localizedDescription
            return
        }
        loadSequence += 1
        let sequence = loadSequence
        isLoading = true
        errorMessage = nil
        var downloaded: [DownloadedReconstructionArtifact] = []
        do {
            let loadedReport = try await reviewClient.loadReview(
                scanID: scanID,
                baseURL: baseURL
            )
            let expectedNames = Set(
                loadedReport.reviewMasks.map {
                    URL(fileURLWithPath: $0).lastPathComponent
                }
            )
            let sampleArtifacts = artifacts
                .filter { $0.isMaskReviewSample && expectedNames.contains($0.filename) }
                .sorted { $0.name < $1.name }
            guard sampleArtifacts.count == loadedReport.reviewMasks.count,
                  Set(sampleArtifacts.map(\.filename)) == expectedNames else {
                throw MaskReviewClientError.invalidPayload
            }
            for artifact in sampleArtifacts {
                try Task.checkCancellation()
                downloaded.append(
                    try await artifactClient.downloadArtifact(
                        artifact,
                        scanID: scanID,
                        baseURL: baseURL
                    )
                )
            }
            guard !Task.isCancelled, sequence == loadSequence else {
                await discard(downloaded)
                return
            }
            report = loadedReport
            samples = downloaded
            if loadedReport.state == .approved {
                approved = true
                decisionMessage = "These masks were already approved. Confirm the 3D region and continue reconstruction."
            } else if loadedReport.state == .rejected {
                rejected = true
                decisionMessage = "These masks were rejected. Correct the saved scan's draft and upload a new job."
            }
            isLoading = false
        } catch is CancellationError {
            await discard(downloaded)
            if sequence == loadSequence { isLoading = false }
        } catch let error as URLError where error.code == .cancelled {
            await discard(downloaded)
            if sequence == loadSequence { isLoading = false }
        } catch {
            await discard(downloaded)
            if sequence == loadSequence {
                isLoading = false
                errorMessage = error.localizedDescription
            }
        }
    }

    func approve(scanID: String, baseURLString: String) async {
        await decide(approve: true, scanID: scanID, baseURLString: baseURLString)
    }

    func reject(scanID: String, baseURLString: String) async {
        await decide(approve: false, scanID: scanID, baseURLString: baseURLString)
    }

    func deactivate() async {
        loadSequence += 1
        isLoading = false
        let downloads = samples
        samples = []
        await discard(downloads)
    }

    private func decide(
        approve: Bool,
        scanID: String,
        baseURLString: String
    ) async {
        guard !isDeciding, !approved, !rejected else { return }
        guard let baseURL = Self.baseURL(from: baseURLString) else {
            errorMessage = ReconstructionJobClientError.invalidBaseURL.localizedDescription
            return
        }
        isDeciding = true
        errorMessage = nil
        decisionMessage = nil
        defer { isDeciding = false }
        do {
            if approve {
                _ = try await reviewClient.approve(scanID: scanID, baseURL: baseURL)
                approved = true
                decisionMessage = "Masks approved. Next, confirm the 3D region and continue reconstruction."
            } else {
                _ = try await reviewClient.reject(scanID: scanID, baseURL: baseURL)
                rejected = true
                decisionMessage = "Masks rejected. Edit the saved scan's mask draft, then upload it as a new job."
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func discard(_ downloads: [DownloadedReconstructionArtifact]) async {
        for download in downloads {
            await artifactClient.discardDownloadedArtifact(download)
        }
    }

    private static func baseURL(from value: String) -> URL? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return URL(string: trimmed)
    }
}

struct MaskReviewView: View {
    let scanID: String
    let baseURLString: String
    let artifacts: [ReconstructionArtifact]
    @StateObject private var store: MaskReviewStore
    @State private var selectedSample = 0
    @State private var confirmApproval = false
    @State private var confirmRejection = false
    @Environment(\.dismiss) private var dismiss

    init(
        scanID: String,
        baseURLString: String,
        artifacts: [ReconstructionArtifact],
        reviewClient: any MaskReviewAccessing,
        artifactClient: any ReconstructionArtifactAccessing
    ) {
        self.scanID = scanID
        self.baseURLString = baseURLString
        self.artifacts = artifacts
        _store = StateObject(
            wrappedValue: MaskReviewStore(
                reviewClient: reviewClient,
                artifactClient: artifactClient
            )
        )
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                explanation
                content
            }
            .padding()
        }
        .navigationTitle("Review Masks")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await store.load(
                scanID: scanID,
                baseURLString: baseURLString,
                artifacts: artifacts
            )
        }
        .onDisappear {
            Task { await store.deactivate() }
        }
        .confirmationDialog(
            "Approve this full mask set?",
            isPresented: $confirmApproval,
            titleVisibility: .visible
        ) {
            Button("Approve All Masks") {
                Task { await store.approve(scanID: scanID, baseURLString: baseURLString) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The five samples represent masks applied across every captured photo.")
        }
        .confirmationDialog(
            "Reject these masks?",
            isPresented: $confirmRejection,
            titleVisibility: .visible
        ) {
            Button("Reject Masks", role: .destructive) {
                Task { await store.reject(scanID: scanID, baseURLString: baseURLString) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("You will need to correct the saved scan's mask draft and upload a new job.")
        }
    }

    private var explanation: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Check what reconstruction will keep", systemImage: "square.stack.3d.up")
                .font(.headline)
            Text("Normal photo color is kept. Dark red is excluded. The cyan line is the mask edge. Check all five points through the scan before deciding.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var content: some View {
        if store.isLoading {
            HStack {
                Spacer()
                ProgressView("Loading mask samples")
                Spacer()
            }
            .padding(.vertical, 60)
        } else if let error = store.errorMessage, store.report == nil {
            ContentUnavailableView(
                "Unable to Load Masks",
                systemImage: "exclamationmark.triangle",
                description: Text(error)
            )
        } else if let report = store.report {
            samples
            quality(report)
            decisionControls(report)
        }
    }

    private var samples: some View {
        VStack(spacing: 10) {
            TabView(selection: $selectedSample) {
                ForEach(Array(store.samples.enumerated()), id: \.element.artifact.id) { index, sample in
                    maskImage(sample)
                        .tag(index)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .always))
            .frame(height: 390)

            Text("Sample \(min(selectedSample + 1, store.samples.count)) of \(store.samples.count)")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func maskImage(_ sample: DownloadedReconstructionArtifact) -> some View {
        if let image = UIImage(contentsOfFile: sample.fileURL.path) {
            Image(uiImage: image)
                .resizable()
                .scaledToFit()
                .frame(maxWidth: .infinity, maxHeight: 350)
                .background(.black)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .accessibilityLabel("Mask review sample \(sample.artifact.displayName)")
        } else {
            ContentUnavailableView("Invalid Sample", systemImage: "photo.badge.exclamationmark")
        }
    }

    private func quality(_ report: MaskReviewReport) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(
                report.state == .needsCorrection ? "Correction Required" : "Automated Checks Passed",
                systemImage: report.state == .needsCorrection
                    ? "exclamationmark.octagon.fill"
                    : "checkmark.shield.fill"
            )
            .font(.headline)
            .foregroundStyle(report.state == .needsCorrection ? .red : .green)

            ForEach(Array(report.quality.blockingIssues.prefix(6))) { issue in
                issueRow(issue, color: .red)
            }
            if report.quality.blockingIssues.count > 6 {
                Text("\(report.quality.blockingIssues.count - 6) more blocking changes were found.")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            ForEach(Array(report.quality.warnings.prefix(6))) { issue in
                issueRow(issue, color: .orange)
            }
            if report.quality.warnings.count > 6 {
                Text("\(report.quality.warnings.count - 6) more low-confidence frames are recorded in the report.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if report.quality.blockingIssues.isEmpty && report.quality.warnings.isEmpty {
                Text("No abrupt area or position changes were detected. Visual review is still required.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .padding()
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }

    private func issueRow(_ issue: MaskReviewIssue, color: Color) -> some View {
        Label {
            Text(issue.message)
                .font(.subheadline)
        } icon: {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(color)
        }
    }

    @ViewBuilder
    private func decisionControls(_ report: MaskReviewReport) -> some View {
        if let message = store.decisionMessage {
            VStack(alignment: .leading, spacing: 12) {
                Label(
                    store.approved ? "Masks Approved" : "Masks Rejected",
                    systemImage: store.approved ? "checkmark.circle.fill" : "xmark.circle.fill"
                )
                .font(.headline)
                .foregroundStyle(store.approved ? .green : .red)
                Text(message)
                    .font(.subheadline)
                Button(store.approved ? "Continue to 3D Region" : "Done") {
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            VStack(spacing: 12) {
                Button {
                    confirmApproval = true
                } label: {
                    Label(
                        store.isDeciding ? "Saving Decision" : "Approve All Masks",
                        systemImage: "checkmark.circle"
                    )
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    store.isDeciding
                        || report.state != .awaitingReview
                        || report.quality.blockingIssueCount > 0
                )

                Button(role: .destructive) {
                    confirmRejection = true
                } label: {
                    Label("Reject and Correct", systemImage: "xmark.circle")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(store.isDeciding)

                if let error = store.errorMessage {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
        }
    }
}
