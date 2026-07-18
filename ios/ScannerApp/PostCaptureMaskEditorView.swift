import SwiftUI
import UIKit

struct MaskEditorFrame: Decodable, Equatable {
    let id: Int
    let image: String
    let resolution: [Int]
}

@MainActor
final class PostCaptureMaskEditorStore: ObservableObject {
    @Published private(set) var frames: [MaskEditorFrame] = []
    @Published private(set) var sampleIndices: [Int] = []
    @Published private(set) var image: UIImage?
    @Published private(set) var isLoading = false
    @Published private(set) var isSaving = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var confirmationMessage: String?
    @Published var selectedSample = 0
    @Published var operation: MaskAuthoringOperation = .keep
    @Published var selections: [Int: [MaskAuthoringRegion]] = [:]

    private let archiveURL: URL
    private let fileManager: FileManager
    private var existingRevision = 0

    init(
        archiveURL: URL,
        fileManager: FileManager = .default
    ) {
        self.archiveURL = archiveURL
        self.fileManager = fileManager
    }

    var selectedFrame: MaskEditorFrame? {
        guard sampleIndices.indices.contains(selectedSample),
              frames.indices.contains(sampleIndices[selectedSample]) else { return nil }
        return frames[sampleIndices[selectedSample]]
    }

    var selectedRegions: [MaskAuthoringRegion] {
        guard let selectedFrame else { return [] }
        return selections[selectedFrame.id] ?? []
    }

    var authoredFrameCount: Int {
        selections.values.filter { !$0.isEmpty }.count
    }

    func load() {
        guard frames.isEmpty else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let scanDirectory = try safeScanDirectory()
            let metadataDirectory = scanDirectory.appendingPathComponent("metadata", isDirectory: true)
            let framesURL = metadataDirectory.appendingPathComponent("frames.json")
            try requireRegularFile(framesURL, maximumBytes: 32 * 1_024 * 1_024)
            let decoded = try JSONDecoder().decode(
                [MaskEditorFrame].self,
                from: Data(contentsOf: framesURL, options: [.mappedIfSafe])
            )
            guard !decoded.isEmpty,
                  Set(decoded.map(\.id)).count == decoded.count,
                  decoded.allSatisfy(validFrame) else {
                throw MaskEditorError.invalidFrames
            }
            frames = decoded
            sampleIndices = MaskAuthoringSampleSelector.representativeIndices(
                frameCount: decoded.count
            )
            try loadExistingPlan(metadataDirectory: metadataDirectory)
            try loadSelectedImage()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func selectSample(_ index: Int) {
        guard sampleIndices.indices.contains(index) else { return }
        selectedSample = index
        do {
            try loadSelectedImage()
            errorMessage = nil
        } catch {
            image = nil
            errorMessage = error.localizedDescription
        }
    }

    func appendRegion(points: [NormalizedMaskPoint]) {
        guard let frame = selectedFrame else { return }
        do {
            try CaptureMaskRasterizer().validate(points)
            guard points.count <= 4_096 else { throw MaskEditorError.invalidPolygon }
            var regions = selections[frame.id] ?? []
            guard regions.count < 64 else { throw MaskEditorError.tooManyRegions }
            regions.append(MaskAuthoringRegion(operation: operation, points: points))
            selections[frame.id] = regions
            confirmationMessage = nil
            errorMessage = nil
        } catch {
            errorMessage = "That shape could not be saved. Draw a simple closed area."
        }
    }

    func undoRegion() {
        guard let frame = selectedFrame, var regions = selections[frame.id], !regions.isEmpty else {
            return
        }
        regions.removeLast()
        selections[frame.id] = regions
        confirmationMessage = nil
    }

    func clearFrame() {
        guard let frame = selectedFrame else { return }
        selections[frame.id] = []
        confirmationMessage = nil
    }

    func save() async -> Bool {
        guard !isSaving else { return false }
        isSaving = true
        errorMessage = nil
        confirmationMessage = nil
        defer { isSaving = false }
        do {
            let authored = frames.compactMap { frame -> MaskAuthoringFrameSelection? in
                guard let regions = selections[frame.id], !regions.isEmpty else { return nil }
                return MaskAuthoringFrameSelection(frameID: frame.id, image: frame.image, regions: regions)
            }
            guard !authored.isEmpty,
                  authored.count <= 16,
                  authored.allSatisfy({ $0.regions.contains(where: { $0.operation == .keep }) }) else {
                throw MaskEditorError.missingKeepRegion
            }
            let plan = MaskAuthoringPlan(
                schemaVersion: "1.0",
                authoringMode: "representative_frames",
                coordinateSpace: "normalized_capture_image",
                maskConvention: "white_keep_black_exclude",
                revision: existingRevision + 1,
                representativeFrames: authored
            )
            let scanDirectory = try safeScanDirectory()
            let planURL = scanDirectory
                .appendingPathComponent("metadata", isDirectory: true)
                .appendingPathComponent("mask_authoring.json")
            let priorData = try? Data(contentsOf: planURL, options: [.mappedIfSafe])
            try await Task.detached(priority: .userInitiated) {
                try Task.checkCancellation()
                do {
                    let writer = ScanPackageWriter()
                    _ = try writer.saveMaskAuthoringPlan(plan, in: scanDirectory)
                    _ = try writer.zipScanFolder(at: scanDirectory)
                } catch {
                    if let priorData {
                        try? priorData.write(to: planURL, options: .atomic)
                    } else {
                        try? FileManager.default.removeItem(at: planURL)
                    }
                    throw error
                }
            }.value
            existingRevision = plan.revision
            confirmationMessage = "Mask draft revision \(plan.revision) saved in the scan ZIP."
            return true
        } catch {
            errorMessage = error.localizedDescription
            return false
        }
    }

    private func loadExistingPlan(metadataDirectory: URL) throws {
        let url = metadataDirectory.appendingPathComponent("mask_authoring.json")
        guard fileManager.fileExists(atPath: url.path) else { return }
        try requireRegularFile(url, maximumBytes: 1_024 * 1_024)
        let plan = try JSONDecoder().decode(
            MaskAuthoringPlan.self,
            from: Data(contentsOf: url, options: [.mappedIfSafe])
        )
        guard plan.schemaVersion == "1.0",
              plan.authoringMode == "representative_frames",
              plan.coordinateSpace == "normalized_capture_image",
              plan.maskConvention == "white_keep_black_exclude",
              plan.revision > 0 else {
            throw MaskEditorError.invalidPlan
        }
        let known = Dictionary(uniqueKeysWithValues: frames.map { ($0.id, $0.image) })
        guard plan.representativeFrames.count <= 16,
              Set(plan.representativeFrames.map(\.frameID)).count == plan.representativeFrames.count else {
            throw MaskEditorError.invalidPlan
        }
        for selection in plan.representativeFrames {
            guard known[selection.frameID] == selection.image,
                  !selection.regions.isEmpty,
                  selection.regions.count <= 64,
                  selection.regions.contains(where: { $0.operation == .keep }) else {
                throw MaskEditorError.invalidPlan
            }
            for region in selection.regions {
                guard region.points.count <= 4_096 else { throw MaskEditorError.invalidPlan }
                try CaptureMaskRasterizer().validate(region.points)
            }
            selections[selection.frameID] = selection.regions
        }
        existingRevision = plan.revision
    }

    private func loadSelectedImage() throws {
        guard let frame = selectedFrame else { throw MaskEditorError.invalidFrames }
        let components = frame.image.split(separator: "/", omittingEmptySubsequences: false)
        guard components.count == 2, components[0] == "images" else {
            throw MaskEditorError.invalidFrames
        }
        let url = try safeScanDirectory().appendingPathComponent(frame.image)
        try requireRegularFile(url, maximumBytes: 100 * 1_024 * 1_024)
        guard let loaded = UIImage(contentsOfFile: url.path), loaded.size.width > 0, loaded.size.height > 0 else {
            throw MaskEditorError.invalidImage
        }
        image = loaded
    }

    private func safeScanDirectory() throws -> URL {
        let directory = archiveURL.deletingPathExtension()
        let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
        guard values.isDirectory == true, values.isSymbolicLink != true else {
            throw MaskEditorError.missingScanDirectory
        }
        return directory
    }

    private func requireRegularFile(_ url: URL, maximumBytes: Int) throws {
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard values.isRegularFile == true,
              values.isSymbolicLink != true,
              let size = values.fileSize,
              size <= maximumBytes else {
            throw MaskEditorError.unsafeFile
        }
    }

    private func validFrame(_ frame: MaskEditorFrame) -> Bool {
        let components = frame.image.split(separator: "/", omittingEmptySubsequences: false)
        return frame.id >= 0
            && components.count == 2
            && components[0] == "images"
            && !components[1].isEmpty
            && components[1] != "."
            && components[1] != ".."
            && !frame.image.contains("\\")
            && frame.resolution.count == 2
            && frame.resolution.allSatisfy { $0 > 0 }
    }

}

private enum MaskEditorError: LocalizedError {
    case missingScanDirectory
    case invalidFrames
    case invalidImage
    case invalidPlan
    case unsafeFile
    case invalidPolygon
    case tooManyRegions
    case missingKeepRegion

    var errorDescription: String? {
        switch self {
        case .missingScanDirectory: return "The editable scan folder is no longer available on this iPhone."
        case .invalidFrames: return "The scan's frame list is invalid."
        case .invalidImage: return "The selected representative photo could not be opened."
        case .invalidPlan: return "The saved mask draft is invalid."
        case .unsafeFile: return "A scan file is missing, too large, or unsafe."
        case .invalidPolygon: return "The drawn area is invalid."
        case .tooManyRegions: return "This frame already has the maximum number of regions."
        case .missingKeepRegion: return "Draw at least one green Keep area before saving. Every edited frame also needs a Keep area."
        }
    }
}

struct PostCaptureMaskEditorView: View {
    let onDone: () -> Void
    @StateObject private var store: PostCaptureMaskEditorStore
    @State private var draftPoints: [NormalizedMaskPoint] = []

    init(archiveURL: URL, onDone: @escaping () -> Void) {
        self.onDone = onDone
        _store = StateObject(wrappedValue: PostCaptureMaskEditorStore(archiveURL: archiveURL))
    }

    var body: some View {
        NavigationStack {
            Group {
                if store.isLoading {
                    ProgressView("Loading representative photos")
                } else if let image = store.image, let frame = store.selectedFrame {
                    editor(image: image, frame: frame)
                } else {
                    ContentUnavailableView(
                        "Mask Draft Unavailable",
                        systemImage: "exclamationmark.triangle",
                        description: Text(store.errorMessage ?? "The scan could not be opened.")
                    )
                }
            }
            .navigationTitle("Scene Mask Draft")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done", action: onDone)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        Task { _ = await store.save() }
                    } label: {
                        if store.isSaving { ProgressView() } else { Text("Save Draft") }
                    }
                    .disabled(store.isSaving || store.authoredFrameCount == 0)
                }
            }
        }
        .task { store.load() }
    }

    private func editor(image: UIImage, frame: MaskEditorFrame) -> some View {
        VStack(spacing: 12) {
            Text("Draft only — full-frame propagation and review happen before these masks can affect reconstruction.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal)

            GeometryReader { geometry in
                let rect = aspectFitRect(imageSize: image.size, container: geometry.size)
                ZStack {
                    Color.black
                    Image(uiImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                    regionOverlay(in: rect)
                    draftPath(in: rect)
                        .stroke(.yellow, style: StrokeStyle(lineWidth: 3, lineJoin: .round))
                }
                .contentShape(Rectangle())
                .gesture(drawGesture(in: rect))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }

            Picker("Drawing mode", selection: $store.operation) {
                Text("Keep").tag(MaskAuthoringOperation.keep)
                Text("Erase").tag(MaskAuthoringOperation.erase)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)

            HStack {
                Button("Previous") { changeSample(by: -1) }
                    .disabled(store.selectedSample == 0)
                Spacer()
                Text("Photo \(store.selectedSample + 1) of \(store.sampleIndices.count)")
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Button("Next") { changeSample(by: 1) }
                    .disabled(store.selectedSample + 1 >= store.sampleIndices.count)
            }
            .padding(.horizontal)

            HStack {
                Button("Undo Area", action: store.undoRegion)
                    .disabled(store.selectedRegions.isEmpty)
                Spacer()
                Text("\(store.selectedRegions.count) areas on this photo")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Clear Photo", role: .destructive, action: store.clearFrame)
                    .disabled(store.selectedRegions.isEmpty)
            }
            .padding(.horizontal)

            if let message = store.errorMessage {
                Text(message).font(.caption).foregroundStyle(.red).padding(.horizontal)
            } else if let message = store.confirmationMessage {
                Text(message).font(.caption).foregroundStyle(.green).padding(.horizontal)
            }
        }
        .padding(.vertical, 8)
        .onChange(of: frame.id) { _, _ in draftPoints = [] }
    }

    private func changeSample(by offset: Int) {
        draftPoints = []
        store.selectSample(store.selectedSample + offset)
    }

    private func drawGesture(in rect: CGRect) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { value in
                guard rect.contains(value.location), rect.width > 0, rect.height > 0 else { return }
                let point = NormalizedMaskPoint(
                    x: Double((value.location.x - rect.minX) / rect.width),
                    y: Double((value.location.y - rect.minY) / rect.height)
                )
                if let previous = draftPoints.last {
                    let dx = CGFloat(point.x - previous.x) * rect.width
                    let dy = CGFloat(point.y - previous.y) * rect.height
                    guard hypot(dx, dy) >= 4 else { return }
                }
                draftPoints.append(point)
            }
            .onEnded { _ in
                if draftPoints.count >= 3 { store.appendRegion(points: draftPoints) }
                draftPoints = []
            }
    }

    private func regionOverlay(in rect: CGRect) -> some View {
        ZStack {
            ForEach(Array(store.selectedRegions.enumerated()), id: \.offset) { _, region in
                polygonPath(region.points, in: rect)
                    .fill(region.operation == .keep ? .green.opacity(0.25) : .red.opacity(0.30))
                polygonPath(region.points, in: rect)
                    .stroke(region.operation == .keep ? .green : .red, lineWidth: 2)
            }
        }
        .allowsHitTesting(false)
    }

    private func draftPath(in rect: CGRect) -> Path {
        polygonPath(draftPoints, in: rect, close: false)
    }

    private func polygonPath(
        _ points: [NormalizedMaskPoint],
        in rect: CGRect,
        close: Bool = true
    ) -> Path {
        Path { path in
            guard let first = points.first else { return }
            path.move(to: screenPoint(first, in: rect))
            for point in points.dropFirst() { path.addLine(to: screenPoint(point, in: rect)) }
            if close && points.count >= 3 { path.closeSubpath() }
        }
    }

    private func screenPoint(_ point: NormalizedMaskPoint, in rect: CGRect) -> CGPoint {
        CGPoint(
            x: rect.minX + CGFloat(point.x) * rect.width,
            y: rect.minY + CGFloat(point.y) * rect.height
        )
    }

    private func aspectFitRect(imageSize: CGSize, container: CGSize) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0 else { return .zero }
        let scale = min(container.width / imageSize.width, container.height / imageSize.height)
        let size = CGSize(width: imageSize.width * scale, height: imageSize.height * scale)
        return CGRect(
            x: (container.width - size.width) / 2,
            y: (container.height - size.height) / 2,
            width: size.width,
            height: size.height
        )
    }
}
