import SwiftUI

private struct ScopeRegionDraft: Equatable {
    var center: [Double]
    var extents: [Double]
    var rotationDegrees: [Double]

    init(bounds: PointCloudBounds) {
        let minimum = bounds.minimum
        let maximum = bounds.maximum
        let rawExtents = maximum - minimum
        let minimumSize = Double(max(bounds.largestExtent * 0.02, 0.001))
        center = [
            Double(bounds.center.x),
            Double(bounds.center.y),
            Double(bounds.center.z),
        ]
        extents = [
            max(Double(rawExtents.x) * 1.05, minimumSize),
            max(Double(rawExtents.y) * 1.05, minimumSize),
            max(Double(rawExtents.z) * 1.05, minimumSize),
        ]
        rotationDegrees = [0, 0, 0]
    }

    init(region: ReconstructionRegion) {
        center = region.center
        extents = region.extents
        rotationDegrees = region.eulerRadians.map { $0 * 180 / .pi }
    }

    func region(revision: Int) throws -> ReconstructionRegion {
        try ReconstructionRegion.userRegion(
            center: center,
            extents: extents,
            eulerRadians: rotationDegrees.map { $0 * .pi / 180 },
            revision: revision
        )
    }
}

struct ScopeRegionEditorView: View {
    let download: DownloadedReconstructionArtifact
    let scanID: String
    let baseURLString: String
    let onDone: () -> Void
    let onStartNewScan: () -> Void
    private let cameraArtifact: ReconstructionArtifact?
    private let artifactClient: any ReconstructionArtifactAccessing

    @StateObject private var previewStore = PointCloudPreviewStore()
    @StateObject private var scopeStore: ReconstructionScopeStore
    @State private var draft: ScopeRegionDraft?
    @State private var pointSize = 3.0
    @State private var reloadSequence = 0
    @State private var cameraPreview: SparseCameraPreview?
    @State private var cameraDownload: DownloadedReconstructionArtifact?
    @State private var cameraErrorMessage: String?

    init(
        download: DownloadedReconstructionArtifact,
        scanID: String,
        baseURLString: String,
        scopeClient: any ReconstructionScopeAccessing,
        cameraArtifact: ReconstructionArtifact?,
        artifactClient: any ReconstructionArtifactAccessing,
        onStartNewScan: @escaping () -> Void,
        onDone: @escaping () -> Void
    ) {
        self.download = download
        self.scanID = scanID
        self.baseURLString = baseURLString
        self.onDone = onDone
        self.onStartNewScan = onStartNewScan
        self.cameraArtifact = cameraArtifact
        self.artifactClient = artifactClient
        _scopeStore = StateObject(wrappedValue: ReconstructionScopeStore(client: scopeClient))
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Reconstruction Region")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Done", action: onDone)
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button {
                            save()
                        } label: {
                            if scopeStore.isSaving || scopeStore.isResuming {
                                ProgressView()
                            } else {
                                Text("Save & Continue")
                            }
                        }
                        .disabled(draft == nil || scopeStore.isSaving || scopeStore.isResuming)
                    }
                }
        }
        .task(id: reloadSequence) {
            await previewStore.load(fileURL: download.fileURL)
            await scopeStore.load(scanID: scanID, baseURLString: baseURLString)
            initializeDraft()
            await loadCameraPreview()
        }
        .onDisappear {
            previewStore.deactivate()
            discardCameraDownload()
        }
    }

    @ViewBuilder
    private var content: some View {
        if let preview = previewStore.preview, let draft {
            editor(preview: preview, draft: draft)
        } else if previewStore.isLoading || scopeStore.isLoading {
            VStack(spacing: 16) {
                ProgressView()
                    .controlSize(.large)
                Text("Preparing sparse reconstruction")
                    .font(.headline)
                Text("The app is loading the point cloud and any previously saved region.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .padding()
        } else {
            ContentUnavailableView {
                Label("Unable to Edit Region", systemImage: "exclamationmark.triangle")
            } description: {
                Text(previewStore.errorMessage ?? scopeStore.errorMessage ?? "The sparse reconstruction could not be loaded.")
            } actions: {
                Button("Try Again") {
                    draft = nil
                    reloadSequence += 1
                }
            }
        }
    }

    private func editor(preview: PointCloudPreview, draft: ScopeRegionDraft) -> some View {
        ScrollView {
            VStack(spacing: 16) {
                PointCloudSceneView(
                    preview: preview,
                    pointSize: Float(pointSize),
                    scopeRegion: try? draft.region(revision: nextRevision),
                    cameraPreview: cameraPreview
                )
                .frame(minHeight: 330)
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .overlay(alignment: .topLeading) {
                    Label("Drag to orbit · Pinch to zoom", systemImage: "move.3d")
                        .font(.caption2)
                        .padding(8)
                        .background(.ultraThinMaterial, in: Capsule())
                        .padding(10)
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text("Keep only what belongs in the finished reconstruction inside the cyan box.")
                        .font(.subheadline)
                    Text("Saving records this region. Processing stays paused until the reconstruction-resume step is available.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if let cameraPreview {
                        Label(
                            "\(cameraPreview.cameraCount) registered viewpoints are shown in orange.",
                            systemImage: "camera.metering.matrix"
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    } else if let cameraErrorMessage {
                        Label(cameraErrorMessage, systemImage: "camera.fill.badge.exclamationmark")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                    HStack(spacing: 14) {
                        Label("Included points", systemImage: "circle.grid.3x3.fill")
                        Label("Excluded points dimmed", systemImage: "circle.dotted")
                        Label("Camera path", systemImage: "camera.fill")
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                if let message = scopeStore.errorMessage {
                    status(message, color: .orange, image: "exclamationmark.triangle.fill")
                } else if let message = scopeStore.confirmationMessage {
                    status(message, color: .green, image: "checkmark.circle.fill")
                }

                GroupBox("Position") {
                    vectorControls(
                        values: binding(for: \.center),
                        ranges: positionRanges(preview: preview, draft: draft),
                        step: controlStep(preview),
                        unit: ""
                    )
                }

                GroupBox("Size") {
                    vectorControls(
                        values: binding(for: \.extents),
                        ranges: sizeRanges(preview: preview, draft: draft),
                        step: controlStep(preview),
                        unit: ""
                    )
                }

                GroupBox("Rotation") {
                    vectorControls(
                        values: binding(for: \.rotationDegrees),
                        ranges: Array(repeating: -180...180, count: 3),
                        step: 1,
                        unit: "°"
                    )
                }

                GroupBox("Preview") {
                    HStack(spacing: 12) {
                        Image(systemName: "circle.fill")
                            .font(.system(size: 7))
                        Slider(value: $pointSize, in: 1...8, step: 0.5)
                            .accessibilityLabel("Point size")
                        Image(systemName: "circle.fill")
                            .font(.system(size: 15))
                    }
                }

                Button {
                    onStartNewScan()
                } label: {
                    Label("Coverage Missing? Capture Again", systemImage: "camera.viewfinder")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)

                HStack {
                    Button("Reset to Sparse Bounds") {
                        self.draft = ScopeRegionDraft(bounds: preview.bounds)
                        scopeStore.clearMessages()
                    }
                    .buttonStyle(.bordered)

                    Button("Save Region") {
                        save()
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(scopeStore.isSaving)
                }
            }
            .padding()
        }
        .background(Color(uiColor: .systemGroupedBackground))
    }

    private func status(_ message: String, color: Color, image: String) -> some View {
        Label(message, systemImage: image)
            .font(.caption)
            .foregroundStyle(color)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(color.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
    }

    private func vectorControls(
        values: [Binding<Double>],
        ranges: [ClosedRange<Double>],
        step: Double,
        unit: String
    ) -> some View {
        VStack(spacing: 8) {
            ForEach(0..<3, id: \.self) { index in
                HStack(spacing: 10) {
                    Text(Self.axisNames[index])
                        .font(.caption.bold())
                        .frame(width: 14)
                    Slider(value: values[index], in: ranges[index], step: step)
                        .accessibilityLabel("\(Self.axisNames[index]) value")
                    Text("\(values[index].wrappedValue, specifier: "%.2f")\(unit)")
                        .font(.caption.monospacedDigit())
                        .frame(width: 70, alignment: .trailing)
                }
            }
        }
        .padding(.top, 4)
    }

    private func binding(
        for keyPath: WritableKeyPath<ScopeRegionDraft, [Double]>
    ) -> [Binding<Double>] {
        (0..<3).map { index in
            Binding(
                get: { draft?[keyPath: keyPath][index] ?? 0 },
                set: { value in
                    draft?[keyPath: keyPath][index] = value
                    scopeStore.clearMessages()
                }
            )
        }
    }

    private func positionRanges(
        preview: PointCloudPreview,
        draft: ScopeRegionDraft
    ) -> [ClosedRange<Double>] {
        let minimum = preview.bounds.minimum
        let maximum = preview.bounds.maximum
        let mins = [Double(minimum.x), Double(minimum.y), Double(minimum.z)]
        let maxes = [Double(maximum.x), Double(maximum.y), Double(maximum.z)]
        let padding = max(Double(preview.bounds.largestExtent) * 0.5, 0.01)
        return (0..<3).map { index in
            let lower = min(mins[index] - padding, draft.center[index])
            let upper = max(maxes[index] + padding, draft.center[index])
            return lower...upper
        }
    }

    private func sizeRanges(
        preview: PointCloudPreview,
        draft: ScopeRegionDraft
    ) -> [ClosedRange<Double>] {
        let minimum = max(Double(preview.bounds.largestExtent) * 0.005, 0.0001)
        let maximum = max(Double(preview.bounds.largestExtent) * 2, minimum * 10)
        return draft.extents.map { minimum...max(maximum, $0) }
    }

    private func controlStep(_ preview: PointCloudPreview) -> Double {
        max(Double(preview.bounds.largestExtent) / 200, 0.0001)
    }

    private var nextRevision: Int {
        (scopeStore.savedRegion?.revision ?? 0) + 1
    }

    private func initializeDraft() {
        guard draft == nil, let preview = previewStore.preview else { return }
        if let region = scopeStore.savedRegion {
            draft = ScopeRegionDraft(region: region)
        } else {
            draft = ScopeRegionDraft(bounds: preview.bounds)
        }
    }

    private func save() {
        guard let draft,
              let region = try? draft.region(revision: nextRevision) else { return }
        Task {
            let saved = await scopeStore.save(
                region,
                scanID: scanID,
                baseURLString: baseURLString
            )
            if saved,
               await scopeStore.resume(scanID: scanID, baseURLString: baseURLString) {
                onDone()
            }
        }
    }

    private func loadCameraPreview() async {
        guard let cameraArtifact,
              let baseURL = URL(string: baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            return
        }
        do {
            let downloaded = try await artifactClient.downloadArtifact(
                cameraArtifact,
                scanID: scanID,
                baseURL: baseURL
            )
            guard !Task.isCancelled else {
                await artifactClient.discardDownloadedArtifact(downloaded)
                return
            }
            cameraDownload = downloaded
            cameraPreview = try SparseCameraPreview.load(fileURL: downloaded.fileURL)
        } catch {
            cameraErrorMessage = "Registered camera coverage could not be loaded."
        }
    }

    private func discardCameraDownload() {
        guard let cameraDownload else { return }
        self.cameraDownload = nil
        Task {
            await artifactClient.discardDownloadedArtifact(cameraDownload)
        }
    }

    private static let axisNames = ["X", "Y", "Z"]
}
