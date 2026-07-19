import SwiftUI

struct ProcessingHistoryView: View {
    @AppStorage("scanner.backendBaseURL") private var backendURLString = "http://localhost:8000"
    @StateObject private var store: ReconstructionJobStore
    private let artifactClient: any ReconstructionArtifactAccessing
    private let scopeClient: any ReconstructionScopeAccessing
    private let maskReviewClient: any MaskReviewAccessing
    private let onStartNewScan: () -> Void

    init(
        jobClient: any ReconstructionJobLoading = HTTPReconstructionJobClient(),
        artifactClient: any ReconstructionArtifactAccessing = HTTPReconstructionArtifactClient(),
        scopeClient: any ReconstructionScopeAccessing = HTTPReconstructionScopeClient(),
        maskReviewClient: any MaskReviewAccessing = HTTPMaskReviewClient(),
        onStartNewScan: @escaping () -> Void = {}
    ) {
        _store = StateObject(
            wrappedValue: ReconstructionJobStore(client: jobClient)
        )
        self.artifactClient = artifactClient
        self.scopeClient = scopeClient
        self.maskReviewClient = maskReviewClient
        self.onStartNewScan = onStartNewScan
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
                                        client: artifactClient,
                                        scopeClient: scopeClient,
                                        maskReviewClient: maskReviewClient,
                                        onStartNewScan: onStartNewScan
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
        case .processing where stage == .awaitingMasks || stage == .awaitingScope:
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
    private let artifactClient: any ReconstructionArtifactAccessing
    private let scopeClient: any ReconstructionScopeAccessing
    private let maskReviewClient: any MaskReviewAccessing
    private let onStartNewScan: () -> Void

    init(
        job: ReconstructionJob,
        baseURLString: String,
        client: any ReconstructionArtifactAccessing,
        scopeClient: any ReconstructionScopeAccessing,
        maskReviewClient: any MaskReviewAccessing,
        onStartNewScan: @escaping () -> Void
    ) {
        self.job = job
        self.baseURLString = baseURLString
        self.artifactClient = client
        self.scopeClient = scopeClient
        self.maskReviewClient = maskReviewClient
        self.onStartNewScan = onStartNewScan
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
                guard store.sharedDownload == nil,
                      store.previewedDownload == nil,
                      store.scopeEditorDownload == nil else { return }
                downloadTask?.cancel()
                downloadTask = nil
                Task {
                    await store.deactivate()
                }
            }
            .sheet(isPresented: sharePresented) {
                shareSheet
            }
            .fullScreenCover(isPresented: previewPresented) {
                pointCloudPreview
            }
            .fullScreenCover(isPresented: scopeEditorPresented) {
                scopeEditor
            }
            .alert("Unable to Open Result", isPresented: errorPresented) {
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

            if job.outputs["mask_generation_report"] != nil {
                Section {
                    NavigationLink {
                        MaskReviewView(
                            scanID: job.scanID,
                            baseURLString: baseURLString,
                            artifacts: store.artifacts,
                            reviewClient: maskReviewClient,
                            artifactClient: artifactClient
                        )
                    } label: {
                        Label {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("Review Masks")
                                    .font(.headline)
                                Text("Required before reconstruction can continue")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        } icon: {
                            Image(systemName: "square.stack.3d.up")
                                .foregroundStyle(.blue)
                        }
                    }
                    .disabled(store.isLoading || maskReviewSamples.isEmpty)
                } header: {
                    Text("Required Review")
                } footer: {
                    if store.isLoading {
                        Text("Loading the five review samples…")
                    } else if maskReviewSamples.isEmpty {
                        Text("The backend did not publish the expected review samples.")
                    }
                }
            }

            Section {
                resultsContent
            } header: {
                Text("Downloadable Results")
            } footer: {
                Text("PLY results can be previewed in the app. Sparse review jobs also let you set the 3D reconstruction region. Temporary downloads are removed when a viewer closes.")
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
        } else if visibleArtifacts.isEmpty {
            ContentUnavailableView(
                "No Downloadable Results",
                systemImage: "shippingbox",
                description: Text(emptyDescription)
            )
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        } else {
            ForEach(visibleArtifacts) { artifact in
                artifactControls(artifact)
            }
        }
    }

    private var visibleArtifacts: [ReconstructionArtifact] {
        store.artifacts.filter { !$0.isMaskReviewSample }
    }

    private var maskReviewSamples: [ReconstructionArtifact] {
        store.artifacts.filter(\.isMaskReviewSample)
    }

    private func artifactControls(_ artifact: ReconstructionArtifact) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ReconstructionArtifactRow(
                artifact: artifact,
                isDownloading: store.isDownloading(artifact)
            )

            if !store.isDownloading(artifact) {
                HStack(spacing: 10) {
                    Spacer()
                    if artifact.supportsPointCloudPreview {
                        Button {
                            startDownload(artifact, destination: .pointCloudPreview)
                        } label: {
                            Label("Preview", systemImage: "eye")
                        }
                    }
                    if job.stage == .awaitingScope && artifact.isSparsePointCloud {
                        Button {
                            startDownload(artifact, destination: .scopeEditor)
                        } label: {
                            Label("Set Region", systemImage: "cube.transparent")
                        }
                    }
                    Button {
                        startDownload(artifact, destination: .share)
                    } label: {
                        Label("Share", systemImage: "square.and.arrow.up")
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(store.downloadingArtifactID != nil)
            }
        }
        .padding(.vertical, 3)
    }

    private func startDownload(
        _ artifact: ReconstructionArtifact,
        destination: ReconstructionArtifactDownloadDestination
    ) {
        guard downloadTask == nil else { return }
        downloadTask = Task {
            await store.download(
                artifact,
                scanID: job.scanID,
                baseURLString: baseURLString,
                destination: destination
            )
            downloadTask = nil
        }
    }

    @ViewBuilder
    private var shareSheet: some View {
        if let download = store.sharedDownload {
            ShareSheet(items: [download.fileURL])
        }
    }

    @ViewBuilder
    private var pointCloudPreview: some View {
        if let download = store.previewedDownload {
            PointCloudPreviewView(download: download) {
                store.dismissPreviewedDownload()
            }
        }
    }

    @ViewBuilder
    private var scopeEditor: some View {
        if let download = store.scopeEditorDownload {
            ScopeRegionEditorView(
                download: download,
                scanID: job.scanID,
                baseURLString: baseURLString,
                scopeClient: scopeClient,
                cameraArtifact: store.artifacts.first(where: { $0.name == "sparse_camera_preview" }),
                artifactClient: artifactClient,
                onStartNewScan: {
                    store.dismissScopeEditorDownload()
                    onStartNewScan()
                }
            ) {
                store.dismissScopeEditorDownload()
            }
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

    private var previewPresented: Binding<Bool> {
        Binding(
            get: { store.previewedDownload != nil },
            set: { isPresented in
                if !isPresented {
                    store.dismissPreviewedDownload()
                }
            }
        )
    }

    private var scopeEditorPresented: Binding<Bool> {
        Binding(
            get: { store.scopeEditorDownload != nil },
            set: { isPresented in
                if !isPresented {
                    store.dismissScopeEditorDownload()
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
            }
        }
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
