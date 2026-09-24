import Foundation

// From the repo root (macOS with Xcode's command-line tools):
// swiftc ios/ScannerApp/ScanMetadataModels.swift ios/ScannerApp/ScanGalleryMetadata.swift \
//   scripts/verify_scan_gallery_metadata.swift -o /tmp/verify-scan-gallery
// /tmp/verify-scan-gallery [optional-path-to-object-session.json]

private enum GalleryVerificationError: Error {
    case failed(String)
}

@main
private enum VerifyScanGalleryMetadata {
    static func main() throws {
        let fileManager = FileManager.default
        let root = fileManager.temporaryDirectory
            .appendingPathComponent("scanner-gallery-verifier-\(UUID().uuidString)")
        try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? fileManager.removeItem(at: root) }

        func fixture(_ name: String, session: Data?) throws -> URL {
            let archive = root.appendingPathComponent(name).appendingPathExtension("zip")
            let metadata = archive.deletingPathExtension().appendingPathComponent("metadata")
            try fileManager.createDirectory(at: metadata, withIntermediateDirectories: true)
            if let session {
                try session.write(to: metadata.appendingPathComponent("session.json"))
            }
            return archive
        }

        func metadataURL(_ archive: URL, _ name: String) -> URL {
            archive.deletingPathExtension().appendingPathComponent("metadata")
                .appendingPathComponent(name)
        }

        let objectJSON = Data(#"{"scan_mode":"object_scan"}"#.utf8)
        let sceneJSON = Data(#"{"scan_mode":"scene_scan"}"#.utf8)
        let object = try fixture("object", session: objectJSON)
        try check(object, mode: .object, title: "Object", symbol: "cube.transparent", foreground: false)
        try Data("{}".utf8).write(to: metadataURL(object, "mask_authoring.json"))
        try check(object, mode: .object, title: "Object", symbol: "cube.transparent", foreground: true)
        print("PASS: Object label is independent of foreground alignment")

        let scene = try fixture("scene", session: sceneJSON)
        try check(scene, mode: .scene, title: "Scene", symbol: "building.2", foreground: false)
        try Data("{}".utf8).write(to: metadataURL(scene, "mask_authoring.json"))
        try check(scene, mode: .scene, title: "Scene", symbol: "building.2", foreground: false)
        print("PASS: Scene label and full-image alignment are preserved")

        let unknownSessions: [(String, Data?)] = [
            ("missing", nil),
            ("legacy", Data("{}".utf8)),
            ("null", Data(#"{"scan_mode":null}"#.utf8)),
            ("future", Data(#"{"scan_mode":"future_mode"}"#.utf8)),
            ("invalid", Data("not JSON".utf8)),
            ("wrong-type", Data(#"{"scan_mode":7}"#.utf8)),
            ("oversized", objectJSON + Data(repeating: 32, count: 65_536)),
        ]
        for (name, session) in unknownSessions {
            let archive = try fixture(name, session: session)
            try Data("{}".utf8).write(to: metadataURL(archive, "mask_authoring.json"))
            try check(archive, mode: nil, title: "Unknown mode", symbol: "questionmark.circle", foreground: false)
        }
        print("PASS: unavailable, invalid, unknown, and oversized metadata never claim Scene mode")

        let linkedSession = try fixture("linked-session", session: nil)
        try fileManager.createSymbolicLink(
            at: metadataURL(linkedSession, "session.json"),
            withDestinationURL: metadataURL(object, "session.json")
        )
        try check(linkedSession, mode: nil, title: "Unknown mode", symbol: "questionmark.circle", foreground: false)
        print("PASS: symbolic-link session metadata is not read")

        let unsafeMasks: [(String, Data?)] = [
            ("empty-mask", Data()),
            ("oversized-mask", Data(repeating: 32, count: 1_048_577)),
            ("linked-mask", nil),
        ]
        for (name, contents) in unsafeMasks {
            let archive = try fixture(name, session: objectJSON)
            let mask = metadataURL(archive, "mask_authoring.json")
            if let contents {
                try contents.write(to: mask)
            } else {
                try fileManager.createSymbolicLink(
                    at: mask,
                    withDestinationURL: metadataURL(object, "mask_authoring.json")
                )
            }
            try check(archive, mode: .object, title: "Object", symbol: "cube.transparent", foreground: false)
        }
        print("PASS: invalid mask files do not change capture labels or enable foreground alignment")

        if let sessionPath = CommandLine.arguments.dropFirst().first {
            let archive = try fixture("device-session", session: Data(contentsOf: URL(fileURLWithPath: sessionPath)))
            try check(archive, mode: .object, title: "Object", symbol: "cube.transparent", foreground: false)
            print("PASS: supplied device session displays Object with full-image alignment")
        }
        print("All scan gallery metadata checks passed")
    }

    private static func check(
        _ archive: URL,
        mode: ScanMode?,
        title: String,
        symbol: String,
        foreground: Bool
    ) throws {
        let metadata = ScanGalleryMetadata.load(for: archive)
        guard metadata.captureMode == mode,
              metadata.captureModeTitle == title,
              metadata.captureModeSymbol == symbol,
              metadata.usesForegroundAlignment == foreground else {
            throw GalleryVerificationError.failed(
                "\(archive.lastPathComponent): expected \(title), foreground=\(foreground); "
                    + "got \(metadata.captureModeTitle), foreground=\(metadata.usesForegroundAlignment)"
            )
        }
    }
}
