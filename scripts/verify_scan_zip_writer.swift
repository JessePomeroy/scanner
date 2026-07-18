import Foundation

private struct MaskAuthoringFixture: Encodable {
    let schemaVersion = "1.0"
    let revision = 1

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case revision
    }
}

@main
struct VerifyScanZipWriter {
    static func main() throws {
        let fileManager = FileManager.default
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("scanner_zip_writer_\(UUID().uuidString)", isDirectory: true)
        let scanId = "scan_zip_writer_test"
        let writer = ScanPackageWriter(rootDirectory: root, fileManager: fileManager)

        defer {
            try? fileManager.removeItem(at: root)
        }

        let scanDirectory = try writer.createNewScanFolder(scanId: scanId)
        let nestedDirectory = scanDirectory
            .appendingPathComponent("metadata", isDirectory: true)
            .appendingPathComponent("nested", isDirectory: true)
        try fileManager.createDirectory(at: nestedDirectory, withIntermediateDirectories: true)
        try fileManager.createDirectory(
            at: scanDirectory.appendingPathComponent("arkit/empty", isDirectory: true),
            withIntermediateDirectories: true
        )

        try Data(#"{"scan_id":"scan_zip_writer_test"}"#.utf8).write(
            to: scanDirectory.appendingPathComponent("metadata/session.json")
        )
        try Data("hello scanner\n".utf8).write(
            to: nestedDirectory.appendingPathComponent("notes.txt")
        )
        let maskData = Data([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x01])
        let maskURL = try writer.saveCaptureMask(
            maskData,
            forImagePath: "images/frame_000001.jpg",
            in: scanDirectory
        )
        assert(maskURL.lastPathComponent == "frame_000001.jpg.png")
        do {
            _ = try writer.saveCaptureMask(
                maskData + Data([0x02]),
                forImagePath: "images/frame_000001.jpg",
                in: scanDirectory
            )
            assertionFailure("Expected an existing capture mask to reject overwrite")
        } catch ScanPackageWriterError.captureMaskAlreadyExists(let existingURL) {
            assert(existingURL == maskURL)
        }
        let storedMaskData = try Data(contentsOf: maskURL)
        assert(storedMaskData == maskData)
        let authoringURL = try writer.saveMaskAuthoringPlan(
            MaskAuthoringFixture(),
            in: scanDirectory
        )
        assert(authoringURL.lastPathComponent == "mask_authoring.json")
        let authoringObject = try JSONSerialization.jsonObject(
            with: Data(contentsOf: authoringURL)
        ) as? [String: Any]
        assert(authoringObject?["schema_version"] as? String == "1.0")

        var largeData = Data()
        for index in 0..<(2 * 1024 * 1024 + 17) {
            largeData.append(UInt8(index % 251))
        }
        try largeData.write(to: scanDirectory.appendingPathComponent("video/scan.mov"))

        let zipURL = try writer.zipScanFolder(at: scanDirectory)
        try validateWithPythonZipfile(zipURL)
        print("Verified scan ZIP: \(zipURL.path)")
    }

    private static func validateWithPythonZipfile(_ zipURL: URL) throws {
        let python = Process()
        python.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        python.arguments = [
            "-c",
            """
            import sys, zipfile
            archive = sys.argv[1]
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                assert bad is None, bad
                names = set(zf.namelist())
                required = {
                    'scan_zip_writer_test/',
                    'scan_zip_writer_test/metadata/session.json',
                    'scan_zip_writer_test/metadata/nested/notes.txt',
                    'scan_zip_writer_test/arkit/empty/',
                    'scan_zip_writer_test/video/scan.mov',
                    'scan_zip_writer_test/masks/capture/frame_000001.jpg.png',
                    'scan_zip_writer_test/metadata/mask_authoring.json',
                }
                missing = required - names
                assert not missing, sorted(missing)
                assert zf.getinfo('scan_zip_writer_test/video/scan.mov').file_size == 2 * 1024 * 1024 + 17
            """,
            zipURL.path
        ]
        try python.run()
        python.waitUntilExit()

        if python.terminationStatus != 0 {
            throw NSError(
                domain: "VerifyScanZipWriter",
                code: Int(python.terminationStatus),
                userInfo: [NSLocalizedDescriptionKey: "Python zipfile validation failed"]
            )
        }
    }
}
