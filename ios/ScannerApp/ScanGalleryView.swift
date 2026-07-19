import SwiftUI

struct ScanGalleryView: View {
    @AppStorage("scanner.backendBaseURL") private var backendURLString = "http://localhost:8000"
    @StateObject private var gallery = ScanGalleryStore()
    @StateObject private var uploadStore: ScanUploadStore
    @State private var shareURL: URL?
    @State private var maskEditorURL: URL?
    @State private var deletionError: String?

    init(uploadClient: ScanUploading = HTTPScanUploadClient()) {
        _uploadStore = StateObject(
            wrappedValue: ScanUploadStore(client: uploadClient)
        )
    }

    var body: some View {
        NavigationStack {
            Group {
                if gallery.scans.isEmpty {
                    ContentUnavailableView(
                        "No Scans",
                        systemImage: "archivebox",
                        description: Text("Exported scan packages will appear here.")
                    )
                } else {
                    List {
                        Section {
                            ForEach(gallery.scans) { scan in
                                HStack(spacing: 12) {
                                    Image(systemName: "archivebox")
                                        .foregroundStyle(.blue)

                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(scan.displayName)
                                            .font(.subheadline.weight(.semibold))
                                            .foregroundStyle(.primary)
                                            .lineLimit(1)
                                            .minimumScaleFactor(0.75)

                                        Text(scan.detailText)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                            .minimumScaleFactor(0.75)

                                        Label(
                                            scan.maskProfile.title,
                                            systemImage: scan.maskProfile == .objectForeground
                                                ? "cube.transparent"
                                                : "building.2"
                                        )
                                        .font(.caption2.weight(.medium))
                                        .foregroundStyle(.blue)
                                    }

                                    Spacer(minLength: 0)

                                    Button {
                                        maskEditorURL = scan.url
                                    } label: {
                                        Image(systemName: "paintbrush.pointed")
                                    }
                                    .disabled(!scan.hasEditableFolder || uploadStore.isUploading)
                                    .buttonStyle(.borderless)
                                    .accessibilityLabel("Edit scene mask draft for \(scan.displayName)")

                                    if uploadStore.isUploading(scan.url) {
                                        ProgressView()
                                            .controlSize(.small)
                                            .accessibilityLabel("Uploading \(scan.displayName)")
                                    } else {
                                        Button {
                                            Task {
                                                await uploadStore.upload(
                                                    archiveURL: scan.url,
                                                    baseURLString: backendURLString,
                                                    maskProfile: scan.maskProfile
                                                )
                                            }
                                        } label: {
                                            Image(systemName: "icloud.and.arrow.up")
                                        }
                                        .disabled(uploadStore.isUploading)
                                        .buttonStyle(.borderless)
                                        .accessibilityLabel("Upload \(scan.displayName)")
                                    }

                                    Button {
                                        shareURL = scan.url
                                    } label: {
                                        Image(systemName: "square.and.arrow.up")
                                    }
                                    .buttonStyle(.borderless)
                                    .accessibilityLabel("Share \(scan.displayName)")
                                }
                                .contentShape(Rectangle())
                            }
                            .onDelete(perform: deleteScans)
                        } footer: {
                            Text("Uploads use the backend URL configured in the Jobs tab: \(backendURLString)")
                        }
                    }
                    .refreshable {
                        gallery.refresh()
                    }
                }
            }
            .navigationTitle("Scans")
            .toolbar {
                if !gallery.scans.isEmpty {
                    ToolbarItem(placement: .topBarLeading) {
                        EditButton()
                    }
                }

                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        gallery.refresh()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .accessibilityLabel("Refresh scans")
                }
            }
            .onAppear {
                gallery.refresh()
            }
            .sheet(
                isPresented: Binding(
                    get: { shareURL != nil },
                    set: { isPresented in
                        if !isPresented {
                            shareURL = nil
                        }
                    }
                )
            ) {
                if let shareURL {
                    ShareSheet(items: [shareURL])
                }
            }
            .fullScreenCover(
                isPresented: Binding(
                    get: { maskEditorURL != nil },
                    set: { isPresented in
                        if !isPresented { maskEditorURL = nil }
                    }
                )
            ) {
                if let maskEditorURL {
                    PostCaptureMaskEditorView(archiveURL: maskEditorURL) {
                        self.maskEditorURL = nil
                        gallery.refresh()
                    }
                }
            }
            .alert(
                "Unable to Delete Scan",
                isPresented: Binding(
                    get: { deletionError != nil },
                    set: { isPresented in
                        if !isPresented {
                            deletionError = nil
                        }
                    }
                )
            ) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(deletionError ?? "The scan could not be removed from this device.")
            }
            .alert(
                uploadStore.notice?.title ?? "Scan Upload",
                isPresented: Binding(
                    get: { uploadStore.notice != nil },
                    set: { isPresented in
                        if !isPresented {
                            uploadStore.clearNotice()
                        }
                    }
                )
            ) {
                Button("OK", role: .cancel) {
                    uploadStore.clearNotice()
                }
            } message: {
                Text(uploadStore.notice?.message ?? "")
            }
        }
    }

    private func deleteScans(at offsets: IndexSet) {
        let deletedURLs = offsets.compactMap { index in
            gallery.scans.indices.contains(index) ? gallery.scans[index].url : nil
        }
        if let uploadingURL = uploadStore.uploadingArchiveURL,
           deletedURLs.contains(uploadingURL) {
            deletionError = "Wait for the current upload to finish before deleting this scan."
            return
        }

        do {
            try gallery.deleteScans(at: offsets)
            if let currentShareURL = shareURL,
               deletedURLs.contains(currentShareURL) {
                shareURL = nil
            }
            if let currentEditorURL = maskEditorURL,
               deletedURLs.contains(currentEditorURL) {
                maskEditorURL = nil
            }
        } catch {
            deletionError = error.localizedDescription
        }
    }
}

struct ScanGalleryItem: Identifiable, Equatable {
    let id: URL
    let url: URL
    let displayName: String
    let createdAt: Date?
    let fileSizeBytes: Int64?
    let hasEditableFolder: Bool
    let maskProfile: ReconstructionMaskProfile

    var detailText: String {
        let dateText = createdAt.map(Self.dateFormatter.string(from:)) ?? "Unknown date"
        let sizeText = fileSizeBytes.map(Self.byteFormatter.string(fromByteCount:)) ?? "Unknown size"
        return "\(dateText) - \(sizeText)"
    }

    private static let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter
    }()

    private static let byteFormatter: ByteCountFormatter = {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useKB, .useMB, .useGB]
        formatter.countStyle = .file
        return formatter
    }()
}

final class ScanGalleryStore: ObservableObject {
    @Published private(set) var scans: [ScanGalleryItem] = []

    private let scanRootDirectory: URL
    private let fileManager: FileManager

    init(
        scanRootDirectory: URL = ScanPackageWriter().rootDirectory,
        fileManager: FileManager = .default
    ) {
        self.scanRootDirectory = scanRootDirectory
        self.fileManager = fileManager
    }

    func refresh() {
        scans = loadScans()
    }

    func deleteScans(at offsets: IndexSet) throws {
        let selectedScans = offsets.compactMap { index in
            scans.indices.contains(index) ? scans[index] : nil
        }

        defer {
            refresh()
        }

        for scan in selectedScans {
            try delete(scan)
        }
    }

    private func loadScans() -> [ScanGalleryItem] {
        guard let urls = try? fileManager.contentsOfDirectory(
            at: scanRootDirectory,
            includingPropertiesForKeys: [.creationDateKey, .contentModificationDateKey, .fileSizeKey],
            options: [.skipsHiddenFiles]
        ) else {
            return []
        }

        return urls
            .filter { $0.pathExtension.lowercased() == "zip" }
            .compactMap(makeGalleryItem)
            .sorted { left, right in
                (left.createdAt ?? .distantPast) > (right.createdAt ?? .distantPast)
            }
    }

    private func makeGalleryItem(for url: URL) -> ScanGalleryItem? {
        guard let values = try? url.resourceValues(
            forKeys: [.creationDateKey, .contentModificationDateKey, .fileSizeKey]
        ) else {
            return nil
        }

        return ScanGalleryItem(
            id: url,
            url: url,
            displayName: url.deletingPathExtension().lastPathComponent,
            createdAt: values.creationDate ?? values.contentModificationDate,
            fileSizeBytes: values.fileSize.map(Int64.init),
            hasEditableFolder: editableFolderExists(for: url),
            maskProfile: maskProfile(for: url)
        )
    }

    private func editableFolderExists(for archiveURL: URL) -> Bool {
        let directory = archiveURL.deletingPathExtension()
        guard let values = try? directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else {
            return false
        }
        return values.isDirectory == true && values.isSymbolicLink != true
    }

    private func maskProfile(for archiveURL: URL) -> ReconstructionMaskProfile {
        let sessionURL = archiveURL
            .deletingPathExtension()
            .appendingPathComponent("metadata", isDirectory: true)
            .appendingPathComponent("session.json", isDirectory: false)
        guard let values = try? sessionURL.resourceValues(
            forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]
        ),
        values.isRegularFile == true,
        values.isSymbolicLink != true,
        let fileSize = values.fileSize,
        (1...65_536).contains(fileSize),
        let data = try? Data(contentsOf: sessionURL, options: [.mappedIfSafe]),
        let session = try? JSONDecoder().decode(GallerySessionMetadata.self, from: data)
        else {
            return .sceneGeometry
        }
        return session.scanMode == "object_scan" ? .objectForeground : .sceneGeometry
    }

    private func delete(_ scan: ScanGalleryItem) throws {
        if fileManager.fileExists(atPath: scan.url.path) {
            try fileManager.removeItem(at: scan.url)
        }

        let extractedScanDirectory = scanRootDirectory
            .appendingPathComponent(scan.displayName, isDirectory: true)
        var isDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: extractedScanDirectory.path, isDirectory: &isDirectory),
           isDirectory.boolValue {
            try fileManager.removeItem(at: extractedScanDirectory)
        }
    }
}

private struct GallerySessionMetadata: Decodable {
    let scanMode: String?

    enum CodingKeys: String, CodingKey {
        case scanMode = "scan_mode"
    }
}

#Preview {
    ScanGalleryView()
}
