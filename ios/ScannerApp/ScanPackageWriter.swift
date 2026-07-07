import Foundation

enum ScanPackageWriterError: Error {
    case invalidScanId
    case missingScanDirectory(URL)
    case unsupportedLargeFile(URL)
    case unsupportedLargeArchive
}

/// Creates and exports the on-device scan package.
///
/// This file will create the scan folder structure, place images/depth/ARKit data
/// in their directories, and zip the completed package for sharing or upload.
final class ScanPackageWriter {
    let rootDirectory: URL

    private let fileManager: FileManager
    private let metadataWriter: MetadataWriter

    init(
        rootDirectory: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Scans", isDirectory: true),
        fileManager: FileManager = .default,
        metadataWriter: MetadataWriter = MetadataWriter()
    ) {
        self.rootDirectory = rootDirectory
        self.fileManager = fileManager
        self.metadataWriter = metadataWriter
    }

    @discardableResult
    func createNewScanFolder(scanId: String) throws -> URL {
        guard isSafeScanId(scanId) else {
            throw ScanPackageWriterError.invalidScanId
        }

        let scanDirectory = rootDirectory.appendingPathComponent(scanId, isDirectory: true)
        let subdirectories = ["images", "depth", "arkit", "metadata", "preview", "video"]

        try fileManager.createDirectory(
            at: scanDirectory,
            withIntermediateDirectories: true
        )

        for subdirectory in subdirectories {
            try fileManager.createDirectory(
                at: scanDirectory.appendingPathComponent(subdirectory, isDirectory: true),
                withIntermediateDirectories: true
            )
        }

        return scanDirectory
    }

    @discardableResult
    func saveFrameMetadata(_ frames: [CapturedFrameMetadata], in scanDirectory: URL) throws -> URL {
        try metadataWriter.writeFrameMetadata(
            frames,
            to: scanDirectory.appendingPathComponent("metadata", isDirectory: true)
        )
    }

    @discardableResult
    func saveSessionMetadata(_ session: ScanSessionMetadata, in scanDirectory: URL) throws -> URL {
        try metadataWriter.writeSessionMetadata(
            session,
            to: scanDirectory.appendingPathComponent("metadata", isDirectory: true)
        )
    }

    @discardableResult
    func saveMotionMetadata(_ samples: [MotionSampleMetadata], in scanDirectory: URL) throws -> URL {
        try metadataWriter.writeMotionMetadata(
            samples,
            to: scanDirectory.appendingPathComponent("metadata", isDirectory: true)
        )
    }

    @discardableResult
    func saveVideoMetadata(_ videos: [VideoCaptureMetadata], in scanDirectory: URL) throws -> URL {
        try metadataWriter.writeVideoMetadata(
            videos,
            to: scanDirectory.appendingPathComponent("metadata", isDirectory: true)
        )
    }

    @discardableResult
    func saveManifest(_ manifest: ScanPackageManifest, in scanDirectory: URL) throws -> URL {
        try metadataWriter.writeManifest(
            manifest,
            to: scanDirectory.appendingPathComponent("metadata", isDirectory: true)
        )
    }

    @discardableResult
    func zipScanFolder(at scanDirectory: URL) throws -> URL {
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: scanDirectory.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            throw ScanPackageWriterError.missingScanDirectory(scanDirectory)
        }

        let destinationURL = scanDirectory
            .deletingPathExtension()
            .appendingPathExtension("zip")

        if fileManager.fileExists(atPath: destinationURL.path) {
            try fileManager.removeItem(at: destinationURL)
        }

        try ZipArchiveWriter(fileManager: fileManager).writeArchive(
            sourceDirectory: scanDirectory,
            destinationURL: destinationURL
        )

        return destinationURL
    }

    private func isSafeScanId(_ scanId: String) -> Bool {
        guard !scanId.isEmpty else { return false }

        let allowedCharacters = CharacterSet.alphanumerics
            .union(CharacterSet(charactersIn: "_-"))

        return scanId.unicodeScalars.allSatisfy { allowedCharacters.contains($0) }
    }
}

private struct ZipArchiveWriter {
    private struct Entry {
        let path: String
        let crc32: UInt32
        let compressedSize: UInt32
        let uncompressedSize: UInt32
        let localHeaderOffset: UInt32
        let isDirectory: Bool
    }

    private let fileManager: FileManager

    init(fileManager: FileManager) {
        self.fileManager = fileManager
    }

    func writeArchive(sourceDirectory: URL, destinationURL: URL) throws {
        let entries = try archiveURLs(for: sourceDirectory)
        var archive = Data()
        var centralDirectoryEntries: [Entry] = []

        for url in entries {
            let isDirectory = try resourceIsDirectory(url)
            let relativePath = try relativeArchivePath(for: url, sourceDirectory: sourceDirectory)
            let fileData = isDirectory ? Data() : try Data(contentsOf: url)

            guard fileData.count <= UInt32.max else {
                throw ScanPackageWriterError.unsupportedLargeFile(url)
            }
            guard archive.count <= UInt32.max else {
                throw ScanPackageWriterError.unsupportedLargeArchive
            }

            let crc = CRC32.checksum(fileData)
            let fileSize = UInt32(fileData.count)
            let localHeaderOffset = UInt32(archive.count)
            let encodedPath = Data(relativePath.utf8)

            archive.appendUInt32(0x04034b50)
            archive.appendUInt16(20)
            archive.appendUInt16(0x0800)
            archive.appendUInt16(0)
            archive.appendUInt16(0)
            archive.appendUInt16(33)
            archive.appendUInt32(crc)
            archive.appendUInt32(fileSize)
            archive.appendUInt32(fileSize)
            archive.appendUInt16(UInt16(encodedPath.count))
            archive.appendUInt16(0)
            archive.append(encodedPath)
            archive.append(fileData)

            centralDirectoryEntries.append(
                Entry(
                    path: relativePath,
                    crc32: crc,
                    compressedSize: fileSize,
                    uncompressedSize: fileSize,
                    localHeaderOffset: localHeaderOffset,
                    isDirectory: isDirectory
                )
            )
        }

        guard archive.count <= UInt32.max else {
            throw ScanPackageWriterError.unsupportedLargeArchive
        }

        let centralDirectoryOffset = UInt32(archive.count)

        for entry in centralDirectoryEntries {
            let encodedPath = Data(entry.path.utf8)
            let externalAttributes: UInt32 = entry.isDirectory ? 0x10 : 0

            archive.appendUInt32(0x02014b50)
            archive.appendUInt16(20)
            archive.appendUInt16(20)
            archive.appendUInt16(0x0800)
            archive.appendUInt16(0)
            archive.appendUInt16(0)
            archive.appendUInt16(33)
            archive.appendUInt32(entry.crc32)
            archive.appendUInt32(entry.compressedSize)
            archive.appendUInt32(entry.uncompressedSize)
            archive.appendUInt16(UInt16(encodedPath.count))
            archive.appendUInt16(0)
            archive.appendUInt16(0)
            archive.appendUInt16(0)
            archive.appendUInt16(0)
            archive.appendUInt32(externalAttributes)
            archive.appendUInt32(entry.localHeaderOffset)
            archive.append(encodedPath)
        }

        guard archive.count <= UInt32.max else {
            throw ScanPackageWriterError.unsupportedLargeArchive
        }

        let centralDirectorySize = UInt32(archive.count) - centralDirectoryOffset
        let entryCount = UInt16(centralDirectoryEntries.count)

        archive.appendUInt32(0x06054b50)
        archive.appendUInt16(0)
        archive.appendUInt16(0)
        archive.appendUInt16(entryCount)
        archive.appendUInt16(entryCount)
        archive.appendUInt32(centralDirectorySize)
        archive.appendUInt32(centralDirectoryOffset)
        archive.appendUInt16(0)

        try archive.write(to: destinationURL, options: [.atomic])
    }

    private func archiveURLs(for sourceDirectory: URL) throws -> [URL] {
        let keys: [URLResourceKey] = [.isDirectoryKey]
        guard let enumerator = fileManager.enumerator(
            at: sourceDirectory,
            includingPropertiesForKeys: keys,
            options: [.skipsHiddenFiles]
        ) else {
            return [sourceDirectory]
        }

        var urls = [sourceDirectory]
        for case let url as URL in enumerator {
            urls.append(url)
        }

        return urls.sorted { $0.path < $1.path }
    }

    private func relativeArchivePath(for url: URL, sourceDirectory: URL) throws -> String {
        let parentDirectory = sourceDirectory.deletingLastPathComponent()
        let parentPath = parentDirectory.standardizedFileURL.path
        let targetPath = url.standardizedFileURL.path

        var relative = String(targetPath.dropFirst(parentPath.count))
        if relative.hasPrefix("/") {
            relative.removeFirst()
        }

        if try resourceIsDirectory(url), !relative.hasSuffix("/") {
            relative.append("/")
        }

        return relative
    }

    private func resourceIsDirectory(_ url: URL) throws -> Bool {
        let values = try url.resourceValues(forKeys: [.isDirectoryKey])
        return values.isDirectory == true
    }
}

private enum CRC32 {
    private static let table: [UInt32] = (0..<256).map { index in
        var value = UInt32(index)
        for _ in 0..<8 {
            if value & 1 == 1 {
                value = 0xedb88320 ^ (value >> 1)
            } else {
                value >>= 1
            }
        }
        return value
    }

    static func checksum(_ data: Data) -> UInt32 {
        var crc: UInt32 = 0xffffffff

        for byte in data {
            let index = Int((crc ^ UInt32(byte)) & 0xff)
            crc = table[index] ^ (crc >> 8)
        }

        return crc ^ 0xffffffff
    }
}

private extension Data {
    mutating func appendUInt16(_ value: UInt16) {
        append(contentsOf: [
            UInt8(value & 0x00ff),
            UInt8((value & 0xff00) >> 8)
        ])
    }

    mutating func appendUInt32(_ value: UInt32) {
        append(contentsOf: [
            UInt8(value & 0x000000ff),
            UInt8((value & 0x0000ff00) >> 8),
            UInt8((value & 0x00ff0000) >> 16),
            UInt8((value & 0xff000000) >> 24)
        ])
    }
}
