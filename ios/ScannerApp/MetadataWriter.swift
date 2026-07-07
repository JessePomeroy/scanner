import Foundation

/// Writes scan metadata JSON files.
///
/// This file will serialize per-frame metadata to metadata/frames.json and
/// session-level metadata to metadata/session.json.
final class MetadataWriter {
    private let encoder: JSONEncoder

    init(encoder: JSONEncoder = MetadataWriter.makeDefaultEncoder()) {
        self.encoder = encoder
    }

    func writeFrameMetadata(_ frames: [CapturedFrameMetadata], to metadataDirectory: URL) throws -> URL {
        try FileManager.default.createDirectory(
            at: metadataDirectory,
            withIntermediateDirectories: true
        )

        let url = metadataDirectory.appendingPathComponent("frames.json")
        let data = try encoder.encode(frames)
        try data.write(to: url, options: [.atomic])
        return url
    }

    func writeSessionMetadata(_ session: ScanSessionMetadata, to metadataDirectory: URL) throws -> URL {
        try FileManager.default.createDirectory(
            at: metadataDirectory,
            withIntermediateDirectories: true
        )

        let url = metadataDirectory.appendingPathComponent("session.json")
        let data = try encoder.encode(session)
        try data.write(to: url, options: [.atomic])
        return url
    }

    func writeMotionMetadata(_ samples: [MotionSampleMetadata], to metadataDirectory: URL) throws -> URL {
        try FileManager.default.createDirectory(
            at: metadataDirectory,
            withIntermediateDirectories: true
        )

        let url = metadataDirectory.appendingPathComponent("imu.json")
        let data = try encoder.encode(samples)
        try data.write(to: url, options: [.atomic])
        return url
    }

    func writeVideoMetadata(_ videos: [VideoCaptureMetadata], to metadataDirectory: URL) throws -> URL {
        try FileManager.default.createDirectory(
            at: metadataDirectory,
            withIntermediateDirectories: true
        )

        let url = metadataDirectory.appendingPathComponent("video.json")
        let data = try encoder.encode(videos)
        try data.write(to: url, options: [.atomic])
        return url
    }

    func writeManifest(_ manifest: ScanPackageManifest, to metadataDirectory: URL) throws -> URL {
        try FileManager.default.createDirectory(
            at: metadataDirectory,
            withIntermediateDirectories: true
        )

        let url = metadataDirectory.appendingPathComponent("manifest.json")
        let data = try encoder.encode(manifest)
        try data.write(to: url, options: [.atomic])
        return url
    }

    private static func makeDefaultEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return encoder
    }
}
