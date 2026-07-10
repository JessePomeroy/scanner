import Foundation

enum PLYVerificationError: Error {
    case assertionFailed(String)
}

@main
struct VerifyPLYPointCloudLoader {
    static func main() async throws {
        let asciiData = Data(
            """
            ply
            format ascii 1.0
            comment synthetic colored cloud
            element vertex 5
            property float x
            property float y
            property float z
            property uchar red
            property uchar green
            property uchar blue
            element face 0
            property list uchar int vertex_indices
            end_header
            -1 0 1 255 0 0
            0 -2 2 0 255 0
            1 1 -3 0 0 255
            2 3 4 128 64 32
            5 6 7 255 255 255
            """.utf8
        )
        let sampled = try PLYPointCloudLoader(maximumPreviewPoints: 2)
            .parse(data: asciiData)
        try require(sampled.sourceVertexCount == 5, "Expected declared vertex count")
        try require(sampled.sampledVertexCount == 2, "Expected deterministic downsampling")
        try require(sampled.isDownsampled, "Expected downsampled marker")
        try require(sampled.hasVertexColors, "Expected RGB detection")
        try require(
            sampled.vertices.map(\.position) == [SIMD3(-1, 0, 1), SIMD3(5, 6, 7)],
            "Expected evenly strided source vertices"
        )
        try require(sampled.bounds.minimum == SIMD3(-1, 0, 1), "Expected sampled minimum")
        try require(sampled.bounds.maximum == SIMD3(5, 6, 7), "Expected sampled maximum")
        try require(
            sampled.vertices[1].color == SIMD4(1, 1, 1, 1),
            "Expected normalized sampled color"
        )

        let binaryData = makeBinaryLittleEndianFixture()
        let binary = try PLYPointCloudLoader(maximumPreviewPoints: 10)
            .parse(data: binaryData)
        try require(binary.sourceVertexCount == 2, "Expected binary vertex count")
        try require(binary.sampledVertexCount == 2, "Expected all binary vertices")
        try require(binary.vertices[0].position == SIMD3(1.5, -2.0, 3.25), "Expected doubles")
        try require(binary.vertices[1].position == SIMD3(-4.0, 5.5, 6.0), "Expected second vertex")
        try require(binary.vertices[1].color == SIMD4(0, 1, 0, 1), "Expected binary RGB")

        let bigEndian = try PLYPointCloudLoader().parse(data: makeBinaryBigEndianFixture())
        try require(
            bigEndian.vertices.single?.position == SIMD3(9.0, 8.0, 7.0),
            "Expected big-endian scalar decoding"
        )
        let boundedBinary = try PLYPointCloudLoader(maximumPreviewPoints: 2)
            .parse(data: makeBinarySamplingFixture())
        try require(
            boundedBinary.vertices.map(\.position) == [SIMD3(0, 0, 0), SIMD3(2, 2, 2)],
            "Expected binary loader to seek directly to its bounded sample"
        )

        let temporaryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanner-ply-verifier-\(UUID().uuidString).ply")
        try asciiData.write(to: temporaryURL)
        defer { try? FileManager.default.removeItem(at: temporaryURL) }
        let filePreview = try await PLYPointCloudLoader(maximumPreviewPoints: 3)
            .load(fileURL: temporaryURL)
        try require(filePreview.sampledVertexCount == 3, "Expected memory-mapped file load")
        do {
            _ = try await PLYPointCloudLoader(maximumFileByteCount: 1)
                .load(fileURL: temporaryURL)
            throw PLYVerificationError.assertionFailed("Expected file-size limit")
        } catch PLYPointCloudError.fileTooLarge {
            // Expected.
        }

        let externalURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanner-ply-external-\(UUID().uuidString).ply")
        let symlinkURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("scanner-ply-link-\(UUID().uuidString).ply")
        try asciiData.write(to: externalURL)
        try FileManager.default.createSymbolicLink(at: symlinkURL, withDestinationURL: externalURL)
        defer {
            try? FileManager.default.removeItem(at: symlinkURL)
            try? FileManager.default.removeItem(at: externalURL)
        }
        do {
            _ = try await PLYPointCloudLoader().load(fileURL: symlinkURL)
            throw PLYVerificationError.assertionFailed("Expected symlink rejection")
        } catch PLYPointCloudError.invalidFile {
            // Expected.
        }

        try expect(
            .unsupportedVertexLayout,
            data: Data(
                """
                ply
                format ascii 1.0
                element vertex 1
                property float x
                property float y
                property float z
                property list uchar float weights
                end_header
                0 0 0 0
                """.utf8
            )
        )
        try expect(
            .fileTooLarge,
            data: Data(
                """
                ply
                format ascii 1.0
                element vertex 5000001
                property float x
                property float y
                property float z
                end_header
                """.utf8
            )
        )
        try expect(
            .unsupportedVertexLayout,
            data: Data(
                """
                ply
                format ascii 1.0
                element vertex 1
                property float x
                property float y
                property float z
                property uchar red
                property uchar green
                end_header
                0 0 0 255 255
                """.utf8
            )
        )
        try expect(
            .invalidVertexData,
            data: Data(
                """
                ply
                format ascii 1.0
                element vertex 1
                property float x
                property float y
                property float z
                property uchar red
                property uchar green
                property uchar blue
                end_header
                0 0 0 300 0 0
                """.utf8
            )
        )
        try expect(
            .invalidVertexData,
            data: Data(
                """
                ply
                format ascii 1.0
                element vertex 2
                property float x
                property float y
                property float z
                end_header
                -3e38 0 0
                3e38 0 0
                """.utf8
            )
        )
        try expect(
            .unsupportedVertexLayout,
            data: Data(
                """
                ply
                format ascii 1.0
                element face 1
                property list uchar int vertex_indices
                element vertex 1
                property float x
                property float y
                property float z
                end_header
                """.utf8
            )
        )

        var truncated = makeBinaryLittleEndianFixture()
        truncated.removeLast()
        try expect(.truncatedPayload, data: truncated)

        print("Verified bounded ASCII and binary PLY point-cloud loading")
    }

    private static func makeBinaryLittleEndianFixture() -> Data {
        let header = """
            ply
            format binary_little_endian 1.0
            element vertex 2
            property double x
            property double y
            property double z
            property float nx
            property uchar red
            property uchar green
            property uchar blue
            end_header
            """ + "\n"
        var data = Data(header.utf8)
        appendDouble(1.5, endianness: .little, to: &data)
        appendDouble(-2.0, endianness: .little, to: &data)
        appendDouble(3.25, endianness: .little, to: &data)
        appendFloat(0.5, endianness: .little, to: &data)
        data.append(contentsOf: [255, 0, 128])
        appendDouble(-4.0, endianness: .little, to: &data)
        appendDouble(5.5, endianness: .little, to: &data)
        appendDouble(6.0, endianness: .little, to: &data)
        appendFloat(-0.25, endianness: .little, to: &data)
        data.append(contentsOf: [0, 255, 0])
        return data
    }

    private static func makeBinaryBigEndianFixture() -> Data {
        let header = """
            ply
            format binary_big_endian 1.0
            element vertex 1
            property float x
            property float y
            property float z
            end_header
            """ + "\n"
        var data = Data(header.utf8)
        appendFloat(9, endianness: .big, to: &data)
        appendFloat(8, endianness: .big, to: &data)
        appendFloat(7, endianness: .big, to: &data)
        return data
    }

    private static func makeBinarySamplingFixture() -> Data {
        let header = """
            ply
            format binary_little_endian 1.0
            element vertex 3
            property float x
            property float y
            property float z
            end_header
            """ + "\n"
        var data = Data(header.utf8)
        for value: Float in [0, 0, 0] {
            appendFloat(value, endianness: .little, to: &data)
        }
        for value: Float in [.nan, .nan, .nan] {
            appendFloat(value, endianness: .little, to: &data)
        }
        for value: Float in [2, 2, 2] {
            appendFloat(value, endianness: .little, to: &data)
        }
        return data
    }

    private enum Endianness {
        case little
        case big
    }

    private static func appendFloat(
        _ value: Float,
        endianness: Endianness,
        to data: inout Data
    ) {
        appendInteger(value.bitPattern, endianness: endianness, to: &data)
    }

    private static func appendDouble(
        _ value: Double,
        endianness: Endianness,
        to data: inout Data
    ) {
        appendInteger(value.bitPattern, endianness: endianness, to: &data)
    }

    private static func appendInteger<T: FixedWidthInteger>(
        _ value: T,
        endianness: Endianness,
        to data: inout Data
    ) {
        var ordered = endianness == .little ? value.littleEndian : value.bigEndian
        withUnsafeBytes(of: &ordered) { bytes in
            data.append(contentsOf: bytes)
        }
    }

    private static func expect(_ error: PLYPointCloudError, data: Data) throws {
        do {
            _ = try PLYPointCloudLoader().parse(data: data)
            throw PLYVerificationError.assertionFailed("Expected \(error)")
        } catch let actual as PLYPointCloudError {
            try require(actual == error, "Expected \(error), received \(actual)")
        }
    }

    private static func require(
        _ condition: @autoclosure () -> Bool,
        _ message: String
    ) throws {
        if !condition() {
            throw PLYVerificationError.assertionFailed(message)
        }
    }
}

private extension Array {
    var single: Element? {
        count == 1 ? self[0] : nil
    }
}
