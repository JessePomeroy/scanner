import SwiftUI

struct ProcessingHistoryView: View {
    @AppStorage("scanner.backendBaseURL") private var backendURLString = "http://localhost:8000"
    @StateObject private var store: ReconstructionJobStore
    private let artifactClient: any ReconstructionArtifactAccessing

    init(
        jobClient: any ReconstructionJobLoading = HTTPReconstructionJobClient(),
        artifactClient: any ReconstructionArtifactAccessing = HTTPReconstructionArtifactClient()
    ) {
        _store = StateObject(
            wrappedValue: ReconstructionJobStore(client: jobClient)
        )
        self.artifactClient = artifactClient
    }

    var body: some View {
        NavigationStack {
            List {
                Section("Backend") {
                    TextField(
                        "Backend URL",
                        text: $backendURLString,
                        prompt: Text("http://192.168.1.10:8000")
                    )
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.go)
                        .onSubmit {
                            refresh()
                        }
                        .accessibilityLabel("Backend URL")

                    Text("On iPhone, use the LAN address of the Mac or PC running the backend.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Button {
                        refresh()
                    } label: {
                        Label(
                            store.isLoading ? "Refreshing" : "Refresh Jobs",
                            systemImage: "arrow.clockwise"
                        )
                    }
                    .disabled(store.isLoading)
                }

                if let errorMessage = store.errorMessage {
                    Section {
                        Label {
                            Text(errorMessage)
                        } icon: {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                        }
                    }
                }

                Section("Recent Jobs") {
                    if store.isLoading && store.jobs.isEmpty {
                        HStack {
                            Spacer()
                            ProgressView("Loading jobs")
                            Spacer()
                        }
                    } else if store.jobs.isEmpty {
                        VStack(spacing: 8) {
                            Image(systemName: "clock.arrow.circlepath")
                                .font(.title2)
                                .foregroundStyle(.secondary)
                            Text(
                                store.hasLoaded
                                    ? "No processing jobs"
                                    : "Refresh to load processing history"
                            )
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 20)
                    } else {
                        ForEach(store.jobs) { job in
                            if job.canOfferArtifacts {
                                NavigationLink {
                                    ReconstructionArtifactListView(
                                        job: job,
                                        baseURLString: backendURLString,
                                        client: artifactClient
                                    )
                                } label: {
                                    ReconstructionJobRow(job: job)
                                }
                            } else {
                                ReconstructionJobRow(job: job)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Processing")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        refresh()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(store.isLoading)
                    .accessibilityLabel("Refresh processing jobs")
                }
            }
            .refreshable {
                await store.refresh(baseURLString: backendURLString)
            }
            .onChange(of: backendURLString) { _, newValue in
                store.backendURLDidChange(to: newValue)
            }
            .task {
                await store.loadIfNeeded(baseURLString: backendURLString)
            }
        }
    }

    private func refresh() {
        Task {
            await store.refresh(baseURLString: backendURLString)
        }
    }
}

private extension ReconstructionJob {
    var canOfferArtifacts: Bool {
        guard !outputs.isEmpty else { return false }
        switch status {
        case .validated, .complete, .failed:
            return true
        case .received, .processing, .unknown:
            return false
        }
    }
}

private struct ReconstructionArtifactListView: View {
    let job: ReconstructionJob
    let baseURLString: String
    @StateObject private var store: ReconstructionArtifactStore
    @State private var downloadTask: Task<Void, Never>?

    init(
        job: ReconstructionJob,
        baseURLString: String,
        client: any ReconstructionArtifactAccessing
    ) {
        self.job = job
        self.baseURLString = baseURLString
        _store = StateObject(
            wrappedValue: ReconstructionArtifactStore(client: client)
        )
    }

    var body: some View {
        resultList
            .navigationTitle("Job Results")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable {
                await refresh()
            }
            .task {
                await store.loadIfNeeded(
                    scanID: job.scanID,
                    baseURLString: baseURLString
                )
            }
            .onDisappear {
                downloadTask?.cancel()
                downloadTask = nil
                Task {
                    await store.deactivate()
                }
            }
            .sheet(isPresented: sharePresented) {
                shareSheet
            }
            .alert("Unable to Load Result", isPresented: errorPresented) {
                Button("OK", role: .cancel) {
                    store.clearError()
                }
            } message: {
                Text(store.errorMessage ?? "The result request failed.")
            }
    }

    private var resultList: some View {
        List {
            Section("Job") {
                ReconstructionJobRow(job: job)
            }

            Section {
                resultsContent
            } header: {
                Text("Downloadable Results")
            } footer: {
                Text("Downloaded results are temporary and removed after the share sheet closes.")
            }
        }
    }

    @ViewBuilder
    private var resultsContent: some View {
        if store.isLoading && store.artifacts.isEmpty {
            HStack {
                Spacer()
                ProgressView("Loading results")
                Spacer()
            }
        } else if store.artifacts.isEmpty {
            ContentUnavailableView(
                "No Downloadable Results",
                systemImage: "shippingbox",
                description: Text(emptyDescription)
            )
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        } else {
            ForEach(store.artifacts) { artifact in
                artifactButton(artifact)
            }
        }
    }

    private func artifactButton(_ artifact: ReconstructionArtifact) -> some View {
        Button {
            guard downloadTask == nil else { return }
            downloadTask = Task {
                await store.download(
                    artifact,
                    scanID: job.scanID,
                    baseURLString: baseURLString
                )
                downloadTask = nil
            }
        } label: {
            ReconstructionArtifactRow(
                artifact: artifact,
                isDownloading: store.isDownloading(artifact)
            )
        }
        .buttonStyle(.plain)
        .disabled(store.downloadingArtifactID != nil)
        .accessibilityLabel("Download and share \(artifact.displayName)")
    }

    @ViewBuilder
    private var shareSheet: some View {
        if let download = store.sharedDownload {
            ShareSheet(items: [download.fileURL])
        }
    }

    private var emptyDescription: String {
        store.hasLoaded
            ? "This job has no published result files."
            : "Refresh to load published result files."
    }

    private var sharePresented: Binding<Bool> {
        Binding(
            get: { store.sharedDownload != nil },
            set: { isPresented in
                if !isPresented {
                    store.dismissSharedDownload()
                }
            }
        )
    }

    private var errorPresented: Binding<Bool> {
        Binding(
            get: { store.errorMessage != nil },
            set: { isPresented in
                if !isPresented {
                    store.clearError()
                }
            }
        )
    }

    private func refresh() async {
        await store.refresh(
            scanID: job.scanID,
            baseURLString: baseURLString
        )
    }
}

private struct ReconstructionArtifactRow: View {
    let artifact: ReconstructionArtifact
    let isDownloading: Bool

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: systemImage)
                .foregroundStyle(.blue)
                .frame(width: 24)

            VStack(alignment: .leading, spacing: 4) {
                Text(artifact.displayName)
                    .font(.subheadline.weight(.semibold))
                Text(artifact.filename)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(
                    "\(Self.byteFormatter.string(fromByteCount: artifact.byteCount)) · "
                        + artifact.mediaType
                )
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }

            Spacer(minLength: 8)

            if isDownloading {
                ProgressView()
                    .controlSize(.small)
            } else {
                Image(systemName: "square.and.arrow.up")
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 3)
    }

    private var systemImage: String {
        switch artifact.filename.split(separator: ".").last?.lowercased() {
        case "obj", "glb", "gltf": return "cube"
        case "ply": return "circle.grid.3x3.fill"
        case "json": return "doc.text.magnifyingglass"
        default: return "doc"
        }
    }

    private static let byteFormatter: ByteCountFormatter = {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useKB, .useMB, .useGB]
        formatter.countStyle = .file
        return formatter
    }()
}

private struct ReconstructionJobRow: View {
    let job: ReconstructionJob

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: job.status.systemImage)
                .foregroundStyle(statusColor)
                .font(.title3)
                .frame(width: 24)

            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(job.stage?.title ?? job.status.title)
                        .font(.subheadline.weight(.semibold))
                    Spacer(minLength: 8)
                    Text(job.status.title)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(statusColor)
                }

                Text(job.scanID)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)

                if let message = job.message, !message.isEmpty {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                HStack(spacing: 12) {
                    if let imageCount = job.imageCount {
                        Label(
                            imageCount == 1 ? "1 image" : "\(imageCount) images",
                            systemImage: "photo"
                        )
                    }
                    if let frameCount = job.frameCount {
                        Label(
                            frameCount == 1 ? "1 frame" : "\(frameCount) frames",
                            systemImage: "viewfinder"
                        )
                    }
                    if let date = job.updatedAt ?? job.createdAt {
                        Text(date, style: .relative)
                    }
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
    }

    private var statusColor: Color {
        switch job.status {
        case .received:
            return .blue
        case .processing:
            return .indigo
        case .validated, .complete:
            return .green
        case .failed:
            return .red
        case .unknown:
            return .secondary
        }
    }
}

#Preview {
    ProcessingHistoryView()
}
