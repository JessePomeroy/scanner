import SwiftUI

struct ScanGalleryView: View {
    @StateObject private var gallery = ScanGalleryStore()
    @State private var shareURL: URL?
    @State private var deletionError: String?

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
                        ForEach(gallery.scans) { scan in
                            Button {
                                shareURL = scan.url
                            } label: {
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
                                    }

                                    Spacer(minLength: 0)

                                    Image(systemName: "square.and.arrow.up")
                                        .foregroundStyle(.secondary)
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Share \(scan.displayName)")
                        }
                        .onDelete(perform: deleteScans)
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
        }
    }

    private func deleteScans(at offsets: IndexSet) {
        let deletedURLs = offsets.compactMap { index in
            gallery.scans.indices.contains(index) ? gallery.scans[index].url : nil
        }

        do {
            try gallery.deleteScans(at: offsets)
            if let currentShareURL = shareURL,
               deletedURLs.contains(currentShareURL) {
                shareURL = nil
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

        for scan in selectedScans {
            try delete(scan)
        }

        refresh()
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
            fileSizeBytes: values.fileSize.map(Int64.init)
        )
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

#Preview {
    ScanGalleryView()
}
