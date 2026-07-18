import Foundation

enum ScanPackageWriterError: Error {
    case invalidScanId
    case missingScanDirectory(URL)
    case unsupportedLargeFile(URL)
    case unsupportedLargeArchive
    case invalidCaptureMaskImagePath
    case invalidCaptureMaskPNG
    case captureMaskAlreadyExists(URL)
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
    func saveCaptureMask(
        _ pngData: Data,
        forImagePath imagePath: String,
        in scanDirectory: URL
    ) throws -> URL {
        let components = imagePath.split(separator: "/", omittingEmptySubsequences: false)
        guard components.count == 2,
              components[0] == "images",
              !components[1].isEmpty,
              components[1] != ".",
              components[1] != "..",
              !imagePath.contains("\\") else {
            throw ScanPackageWriterError.invalidCaptureMaskImagePath
        }
        let pngSignature = Data([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
        guard pngData.count >= pngSignature.count,
              pngData.prefix(pngSignature.count) == pngSignature else {
            throw ScanPackageWriterError.invalidCaptureMaskPNG
        }
        let captureDirectory = scanDirectory
            .appendingPathComponent("masks", isDirectory: true)
            .appendingPathComponent("capture", isDirectory: true)
        try fileManager.createDirectory(at: captureDirectory, withIntermediateDirectories: true)
        let destination = captureDirectory.appendingPathComponent(String(components[1]) + ".png")
        guard !fileManager.fileExists(atPath: destination.path) else {
            throw ScanPackageWriterError.captureMaskAlreadyExists(destination)
        }
        try pngData.write(to: destination, options: .withoutOverwriting)
        return destination
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
        let temporaryURL = destinationURL
            .deletingLastPathComponent()
            .appendingPathComponent(".\(destinationURL.lastPathComponent).\(UUID().uuidString).tmp")

        do {
            try ZipArchiveWriter(fileManager: fileManager).writeArchive(
                sourceDirectory: scanDirectory,
                destinationURL: temporaryURL
            )

            if fileManager.fileExists(atPath: destinationURL.path) {
                try fileManager.removeItem(at: destinationURL)
            }

            try fileManager.moveItem(at: temporaryURL, to: destinationURL)
        } catch {
            if fileManager.fileExists(atPath: temporaryURL.path) {
                try? fileManager.removeItem(at: temporaryURL)
            }

            throw error
        }

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
        var centralDirectoryEntries: [Entry] = []
        var archiveOffset: UInt32 = 0

        _ = fileManager.createFile(atPath: destinationURL.path, contents: nil)
        let archive = try FileHandle(forWritingTo: destinationURL)
        defer {
            try? archive.close()
        }

        for url in entries {
            let isDirectory = try resourceIsDirectory(url)
            let relativePath = try relativeArchivePath(for: url, sourceDirectory: sourceDirectory)
            let fileSize = isDirectory ? 0 : try uint32FileSize(url)
            let crc = isDirectory ? 0 : try CRC32.checksum(fileAt: url)

            let localHeaderOffset = archiveOffset
            let encodedPath = try encodedArchivePath(relativePath)
            let header = localFileHeader(
                encodedPath: encodedPath,
                crc: crc,
                fileSize: fileSize
            )

            try write(header, to: archive, offset: &archiveOffset)
            if !isDirectory {
                try writeFileData(url, to: archive, offset: &archiveOffset)
            }

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

        let centralDirectoryOffset = archiveOffset

        for entry in centralDirectoryEntries {
            try write(
                try centralDirectoryHeader(for: entry),
                to: archive,
                offset: &archiveOffset
            )
        }

        let centralDirectorySize = archiveOffset - centralDirectoryOffset
        let endRecord = try endOfCentralDirectoryRecord(
            entryCount: centralDirectoryEntries.count,
            centralDirectorySize: centralDirectorySize,
            centralDirectoryOffset: centralDirectoryOffset
        )
        try write(endRecord, to: archive, offset: &archiveOffset)
    }

    private func localFileHeader(encodedPath: Data, crc: UInt32, fileSize: UInt32) -> Data {
        var header = Data()
        header.appendUInt32(0x04034b50)
        header.appendUInt16(20)
        header.appendUInt16(0x0800)
        header.appendUInt16(0)
        header.appendUInt16(0)
        header.appendUInt16(33)
        header.appendUInt32(crc)
        header.appendUInt32(fileSize)
        header.appendUInt32(fileSize)
        header.appendUInt16(UInt16(encodedPath.count))
        header.appendUInt16(0)
        header.append(encodedPath)
        return header
    }

    private func centralDirectoryHeader(for entry: Entry) throws -> Data {
        let encodedPath = try encodedArchivePath(entry.path)
        let externalAttributes: UInt32 = entry.isDirectory ? 0x10 : 0
        var header = Data()

        header.appendUInt32(0x02014b50)
        header.appendUInt16(20)
        header.appendUInt16(20)
        header.appendUInt16(0x0800)
        header.appendUInt16(0)
        header.appendUInt16(0)
        header.appendUInt16(33)
        header.appendUInt32(entry.crc32)
        header.appendUInt32(entry.compressedSize)
        header.appendUInt32(entry.uncompressedSize)
        header.appendUInt16(UInt16(encodedPath.count))
        header.appendUInt16(0)
        header.appendUInt16(0)
        header.appendUInt16(0)
        header.appendUInt16(0)
        header.appendUInt32(externalAttributes)
        header.appendUInt32(entry.localHeaderOffset)
        header.append(encodedPath)
        return header
    }

    private func encodedArchivePath(_ path: String) throws -> Data {
        let encodedPath = Data(path.utf8)
        guard encodedPath.count <= UInt16.max else {
            throw ScanPackageWriterError.unsupportedLargeArchive
        }

        return encodedPath
    }

    private func endOfCentralDirectoryRecord(
        entryCount: Int,
        centralDirectorySize: UInt32,
        centralDirectoryOffset: UInt32
    ) throws -> Data {
        guard entryCount <= UInt16.max else {
            throw ScanPackageWriterError.unsupportedLargeArchive
        }

        let entryCount = UInt16(entryCount)
        var record = Data()
        record.appendUInt32(0x06054b50)
        record.appendUInt16(0)
        record.appendUInt16(0)
        record.appendUInt16(entryCount)
        record.appendUInt16(entryCount)
        record.appendUInt32(centralDirectorySize)
        record.appendUInt32(centralDirectoryOffset)
        record.appendUInt16(0)
        return record
    }

    private func write(_ data: Data, to archive: FileHandle, offset: inout UInt32) throws {
        try advanceOffset(by: UInt64(data.count), offset: &offset)
        try archive.write(contentsOf: data)
    }

    private func writeFileData(_ url: URL, to archive: FileHandle, offset: inout UInt32) throws {
        let source = try FileHandle(forReadingFrom: url)
        defer {
            try? source.close()
        }

        while true {
            let data = try source.read(upToCount: 1024 * 1024) ?? Data()
            if data.isEmpty {
                break
            }

            try write(data, to: archive, offset: &offset)
        }
    }

    private func uint32FileSize(_ url: URL) throws -> UInt32 {
        let values = try url.resourceValues(forKeys: [.fileSizeKey])
        guard let fileSize = values.fileSize,
              fileSize >= 0,
              fileSize <= UInt32.max else {
            throw ScanPackageWriterError.unsupportedLargeFile(url)
        }

        return UInt32(fileSize)
    }

    private func advanceOffset(by byteCount: UInt64, offset: inout UInt32) throws {
        guard UInt64(offset) + byteCount <= UInt64(UInt32.max) else {
            throw ScanPackageWriterError.unsupportedLargeArchive
        }

        offset += UInt32(byteCount)
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

        update(&crc, with: data)
        return crc ^ 0xffffffff
    }

    static func checksum(fileAt url: URL) throws -> UInt32 {
        let file = try FileHandle(forReadingFrom: url)
        defer {
            try? file.close()
        }

        var crc: UInt32 = 0xffffffff
        while true {
            let data = try file.read(upToCount: 1024 * 1024) ?? Data()
            if data.isEmpty {
                break
            }

            update(&crc, with: data)
        }

        return crc ^ 0xffffffff
    }

    private static func update(_ crc: inout UInt32, with data: Data) {
        for byte in data {
            let index = Int((crc ^ UInt32(byte)) & 0xff)
            crc = table[index] ^ (crc >> 8)
        }
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
