import Foundation

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
