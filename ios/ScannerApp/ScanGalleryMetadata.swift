import Foundation

struct ScanGalleryMetadata: Equatable {
    let captureMode: ScanMode?
    let usesForegroundAlignment: Bool

    var captureModeTitle: String {
        captureMode?.title ?? "Unknown mode"
    }

    var captureModeSymbol: String {
        switch captureMode {
        case .object: return "cube.transparent"
        case .scene: return "building.2"
        case nil: return "questionmark.circle"
        }
    }

    static func load(for archiveURL: URL) -> Self {
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
        let session = try? JSONDecoder().decode(SessionMetadata.self, from: data)
        else {
            return Self(captureMode: nil, usesForegroundAlignment: false)
        }

        let mode = session.scanMode.flatMap(ScanMode.init(rawValue:))
        // An Object capture can still use full-image alignment when it has no
        // mask draft. That reconstruction choice must not relabel the capture.
        return Self(
            captureMode: mode,
            usesForegroundAlignment: mode == .object && hasSafeMaskAuthoringPlan(for: archiveURL)
        )
    }

    private static func hasSafeMaskAuthoringPlan(for archiveURL: URL) -> Bool {
        let authoringURL = archiveURL
            .deletingPathExtension()
            .appendingPathComponent("metadata", isDirectory: true)
            .appendingPathComponent("mask_authoring.json", isDirectory: false)
        guard let values = try? authoringURL.resourceValues(
            forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]
        ),
        values.isRegularFile == true,
        values.isSymbolicLink != true,
        let fileSize = values.fileSize
        else {
            return false
        }
        return (1...1_048_576).contains(fileSize)
    }

    private struct SessionMetadata: Decodable {
        let scanMode: String?

        enum CodingKeys: String, CodingKey {
            case scanMode = "scan_mode"
        }
    }
}
