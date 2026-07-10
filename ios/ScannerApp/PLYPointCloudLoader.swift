import Foundation

struct PointCloudBounds: Equatable, Sendable {
    let minimum: SIMD3<Float>
    let maximum: SIMD3<Float>

    var center: SIMD3<Float> {
        (minimum / 2) + (maximum / 2)
    }

    var largestExtent: Float {
        let extent = maximum - minimum
        return max(extent.x, max(extent.y, extent.z))
    }
}

struct PointCloudPreviewVertex: Equatable, Sendable {
    let position: SIMD3<Float>
    let color: SIMD4<Float>
}

struct PointCloudPreview: Identifiable, Equatable, Sendable {
    let id: UUID
    let vertices: [PointCloudPreviewVertex]
    let sourceVertexCount: Int
    let bounds: PointCloudBounds
    let hasVertexColors: Bool

    var sampledVertexCount: Int { vertices.count }
    var isDownsampled: Bool { sampledVertexCount < sourceVertexCount }
}

enum PLYPointCloudError: LocalizedError, Equatable {
    case invalidFile
    case fileTooLarge
    case invalidHeader
    case unsupportedFormat
    case unsupportedVertexLayout
    case invalidVertexCount
    case truncatedPayload
    case invalidVertexData
    case noRenderableVertices

    var errorDescription: String? {
        switch self {
        case .invalidFile:
            return "The point cloud is not a regular local file."
        case .fileTooLarge:
            return "The point cloud is too large for the on-device preview."
        case .invalidHeader:
            return "The file does not contain a valid PLY header."
        case .unsupportedFormat:
            return "This PLY encoding is not supported by the preview."
        case .unsupportedVertexLayout:
            return "This PLY vertex layout is not supported by the preview."
        case .invalidVertexCount:
            return "The PLY declares an invalid number of vertices."
        case .truncatedPayload:
            return "The PLY ended before all declared vertices were read."
        case .invalidVertexData:
            return "The PLY contains invalid vertex coordinates or colors."
        case .noRenderableVertices:
            return "The PLY does not contain any renderable vertices."
        }
    }
}

struct PLYPointCloudLoader: Sendable {
    static let defaultMaximumPreviewPoints = 120_000
    static let defaultMaximumFileByteCount: Int64 = 2 * 1_024 * 1_024 * 1_024

    let maximumPreviewPoints: Int
    let maximumFileByteCount: Int64

    init(
        maximumPreviewPoints: Int = Self.defaultMaximumPreviewPoints,
        maximumFileByteCount: Int64 = Self.defaultMaximumFileByteCount
    ) {
        self.maximumPreviewPoints = maximumPreviewPoints
        self.maximumFileByteCount = maximumFileByteCount
    }

    func load(fileURL: URL) async throws -> PointCloudPreview {
        let loadingTask = Task.detached(priority: .userInitiated) {
            try Task.checkCancellation()
            guard self.maximumPreviewPoints > 0,
                  self.maximumFileByteCount > 0,
                  fileURL.isFileURL else {
                throw PLYPointCloudError.invalidFile
            }

            let values = try fileURL.resourceValues(
                forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey]
            )
            guard values.isRegularFile == true,
                  values.isSymbolicLink != true,
                  let fileSize = values.fileSize else {
                throw PLYPointCloudError.invalidFile
            }
            guard Int64(fileSize) <= self.maximumFileByteCount else {
                throw PLYPointCloudError.fileTooLarge
            }

            let data: Data
            do {
                data = try Data(contentsOf: fileURL, options: [.alwaysMapped])
            } catch {
                throw PLYPointCloudError.invalidFile
            }
            try Task.checkCancellation()
            return try self.parse(data: data)
        }

        return try await withTaskCancellationHandler {
            try await loadingTask.value
        } onCancel: {
            loadingTask.cancel()
        }
    }

    func parse(data: Data) throws -> PointCloudPreview {
        guard maximumPreviewPoints > 0 else {
            throw PLYPointCloudError.invalidVertexCount
        }
        let header = try Header.parse(data: data)
        guard header.vertexCount > 0,
              header.vertexCount <= Header.maximumVertexCount else {
            throw PLYPointCloudError.invalidVertexCount
        }
        if header.format == .ascii {
            guard header.vertexCount <= Header.maximumASCIIVertexCount,
                  data.count <= Header.maximumASCIIFileByteCount else {
                throw PLYPointCloudError.fileTooLarge
            }
        }

        let samplePlan = SamplePlan(
            sourceCount: header.vertexCount,
            maximumSampleCount: maximumPreviewPoints
        )
        var accumulator = VertexAccumulator(
            capacity: samplePlan.sampleCount,
            hasVertexColors: header.hasVertexColors
        )

        switch header.format {
        case .ascii:
            try parseASCII(
                data: data,
                header: header,
                samplePlan: samplePlan,
                accumulator: &accumulator
            )
        case .binaryLittleEndian, .binaryBigEndian:
            try parseBinary(
                data: data,
                header: header,
                samplePlan: samplePlan,
                accumulator: &accumulator
            )
        }

        guard let bounds = accumulator.bounds,
              !accumulator.vertices.isEmpty else {
            throw PLYPointCloudError.noRenderableVertices
        }
        guard bounds.center.x.isFinite,
              bounds.center.y.isFinite,
              bounds.center.z.isFinite,
              bounds.largestExtent.isFinite else {
            throw PLYPointCloudError.invalidVertexData
        }
        return PointCloudPreview(
            id: UUID(),
            vertices: accumulator.vertices,
            sourceVertexCount: header.vertexCount,
            bounds: bounds,
            hasVertexColors: header.hasVertexColors
        )
    }

    private func parseASCII(
        data: Data,
        header: Header,
        samplePlan: SamplePlan,
        accumulator: inout VertexAccumulator
    ) throws {
        var cursor = header.payloadOffset
        var sampleNumber = 0
        var nextSampleIndex = samplePlan.sourceIndex(forSampleNumber: sampleNumber)
        var nextCancellationOffset = cursor + 65_536
        for vertexIndex in 0..<header.vertexCount {
            guard let lineRange = try Self.nextLineRange(
                in: data,
                cursor: &cursor,
                maximumByteCount: 1_048_576
            ) else {
                throw PLYPointCloudError.truncatedPayload
            }
            if cursor >= nextCancellationOffset {
                try Task.checkCancellation()
                nextCancellationOffset = cursor + 65_536
            }
            guard vertexIndex == nextSampleIndex else { continue }

            try Task.checkCancellation()
            let line = String(decoding: data[lineRange], as: UTF8.self)
            let values = line.split(whereSeparator: \Character.isWhitespace)
            guard values.count == header.properties.count else {
                throw PLYPointCloudError.invalidVertexData
            }

            var components = VertexComponents()
            for (propertyIndex, property) in header.properties.enumerated() {
                guard let value = Double(values[propertyIndex]),
                      value.isFinite,
                      property.type.acceptsASCII(value) else {
                    throw PLYPointCloudError.invalidVertexData
                }
                components.assign(value, semantic: property.semantic)
            }
            try accumulator.include(
                components,
                header: header
            )
            sampleNumber += 1
            if sampleNumber < samplePlan.sampleCount {
                nextSampleIndex = samplePlan.sourceIndex(forSampleNumber: sampleNumber)
            }
        }
    }

    private func parseBinary(
        data: Data,
        header: Header,
        samplePlan: SamplePlan,
        accumulator: inout VertexAccumulator
    ) throws {
        let recordByteCount = header.properties.reduce(0) { $0 + $1.type.byteCount }
        guard recordByteCount > 0 else {
            throw PLYPointCloudError.unsupportedVertexLayout
        }
        let (payloadByteCount, overflow) = header.vertexCount
            .multipliedReportingOverflow(by: recordByteCount)
        guard !overflow,
              header.payloadOffset <= data.count,
              payloadByteCount <= data.count - header.payloadOffset else {
            throw PLYPointCloudError.truncatedPayload
        }

        let decodedProperties = header.properties.filter { $0.semantic != .ignored }
        try data.withUnsafeBytes { bytes in
            for sampleNumber in 0..<samplePlan.sampleCount {
                try Task.checkCancellation()
                let vertexIndex = samplePlan.sourceIndex(forSampleNumber: sampleNumber)
                let recordOffset = header.payloadOffset + (vertexIndex * recordByteCount)
                var components = VertexComponents()
                for property in decodedProperties {
                    let value = try property.type.decode(
                        bytes: bytes,
                        offset: recordOffset + property.byteOffset,
                        format: header.format
                    )
                    guard value.isFinite else {
                        throw PLYPointCloudError.invalidVertexData
                    }
                    components.assign(value, semantic: property.semantic)
                }
                try accumulator.include(
                    components,
                    header: header
                )
            }
        }
    }

    private static func nextLineRange(
        in data: Data,
        cursor: inout Int,
        maximumByteCount: Int
    ) throws -> Range<Int>? {
        guard cursor < data.count else { return nil }
        let start = cursor
        var nextCancellationOffset = start + 65_536
        while cursor < data.count, data[cursor] != 0x0A {
            cursor += 1
            if cursor >= nextCancellationOffset {
                try Task.checkCancellation()
                nextCancellationOffset += 65_536
            }
            guard cursor - start <= maximumByteCount else {
                throw PLYPointCloudError.invalidVertexData
            }
        }
        let rawEnd = cursor
        if cursor < data.count {
            cursor += 1
        }
        var end = rawEnd
        if end > start, data[end - 1] == 0x0D {
            end -= 1
        }
        return start..<end
    }

    private static func nextLine(
        in data: Data,
        cursor: inout Int,
        maximumByteCount: Int
    ) throws -> String? {
        guard let range = try nextLineRange(
            in: data,
            cursor: &cursor,
            maximumByteCount: maximumByteCount
        ) else { return nil }
        return String(decoding: data[range], as: UTF8.self)
    }
}

private extension PLYPointCloudLoader {
    enum Format: Equatable {
        case ascii
        case binaryLittleEndian
        case binaryBigEndian
    }

    enum PropertySemantic: Equatable {
        case x
        case y
        case z
        case red
        case green
        case blue
        case alpha
        case ignored

        init(name: String) {
            switch name.lowercased() {
            case "x": self = .x
            case "y": self = .y
            case "z": self = .z
            case "red", "r": self = .red
            case "green", "g": self = .green
            case "blue", "b": self = .blue
            case "alpha", "a": self = .alpha
            default: self = .ignored
            }
        }
    }

    enum ScalarType: Equatable {
        case int8
        case uint8
        case int16
        case uint16
        case int32
        case uint32
        case float32
        case float64

        init?(name: String) {
            switch name.lowercased() {
            case "char", "int8": self = .int8
            case "uchar", "uint8": self = .uint8
            case "short", "int16": self = .int16
            case "ushort", "uint16": self = .uint16
            case "int", "int32": self = .int32
            case "uint", "uint32": self = .uint32
            case "float", "float32": self = .float32
            case "double", "float64": self = .float64
            default: return nil
            }
        }

        var byteCount: Int {
            switch self {
            case .int8, .uint8: return 1
            case .int16, .uint16: return 2
            case .int32, .uint32, .float32: return 4
            case .float64: return 8
            }
        }

        func decode(
            bytes: UnsafeRawBufferPointer,
            offset: Int,
            format: Format
        ) throws -> Double {
            guard offset >= 0, byteCount <= bytes.count - offset else {
                throw PLYPointCloudError.truncatedPayload
            }
            switch self {
            case .int8:
                return Double(Int8(bitPattern: bytes[offset]))
            case .uint8:
                return Double(bytes[offset])
            case .int16:
                return Double(Int16(bitPattern: orderedUInt16(bytes, offset, format)))
            case .uint16:
                return Double(orderedUInt16(bytes, offset, format))
            case .int32:
                return Double(Int32(bitPattern: orderedUInt32(bytes, offset, format)))
            case .uint32:
                return Double(orderedUInt32(bytes, offset, format))
            case .float32:
                return Double(Float(bitPattern: orderedUInt32(bytes, offset, format)))
            case .float64:
                return Double(bitPattern: orderedUInt64(bytes, offset, format))
            }
        }

        private func orderedUInt16(
            _ bytes: UnsafeRawBufferPointer,
            _ offset: Int,
            _ format: Format
        ) -> UInt16 {
            let value = bytes.loadUnaligned(fromByteOffset: offset, as: UInt16.self)
            return format == .binaryLittleEndian
                ? UInt16(littleEndian: value)
                : UInt16(bigEndian: value)
        }

        private func orderedUInt32(
            _ bytes: UnsafeRawBufferPointer,
            _ offset: Int,
            _ format: Format
        ) -> UInt32 {
            let value = bytes.loadUnaligned(fromByteOffset: offset, as: UInt32.self)
            return format == .binaryLittleEndian
                ? UInt32(littleEndian: value)
                : UInt32(bigEndian: value)
        }

        private func orderedUInt64(
            _ bytes: UnsafeRawBufferPointer,
            _ offset: Int,
            _ format: Format
        ) -> UInt64 {
            let value = bytes.loadUnaligned(fromByteOffset: offset, as: UInt64.self)
            return format == .binaryLittleEndian
                ? UInt64(littleEndian: value)
                : UInt64(bigEndian: value)
        }

        func acceptsASCII(_ value: Double) -> Bool {
            switch self {
            case .int8:
                return value.rounded() == value
                    && value >= Double(Int8.min)
                    && value <= Double(Int8.max)
            case .uint8:
                return value.rounded() == value
                    && value >= 0
                    && value <= Double(UInt8.max)
            case .int16:
                return value.rounded() == value
                    && value >= Double(Int16.min)
                    && value <= Double(Int16.max)
            case .uint16:
                return value.rounded() == value
                    && value >= 0
                    && value <= Double(UInt16.max)
            case .int32:
                return value.rounded() == value
                    && value >= Double(Int32.min)
                    && value <= Double(Int32.max)
            case .uint32:
                return value.rounded() == value
                    && value >= 0
                    && value <= Double(UInt32.max)
            case .float32:
                return Float(value).isFinite
            case .float64:
                return true
            }
        }

        func normalizedColor(_ value: Double) -> Float? {
            let normalized: Double
            switch self {
            case .uint8:
                normalized = value / 255
            case .uint16:
                normalized = value / 65_535
            case .uint32:
                normalized = value / Double(UInt32.max)
            case .int8:
                guard value >= 0 else { return nil }
                normalized = value / Double(Int8.max)
            case .int16:
                guard value >= 0 else { return nil }
                normalized = value / Double(Int16.max)
            case .int32:
                guard value >= 0 else { return nil }
                normalized = value / Double(Int32.max)
            case .float32, .float64:
                normalized = value > 1 ? value / 255 : value
            }
            guard normalized.isFinite, (0...1).contains(normalized) else { return nil }
            return Float(normalized)
        }
    }

    struct Property: Equatable {
        let type: ScalarType
        let semantic: PropertySemantic
        let byteOffset: Int
    }

    struct Header: Equatable {
        static let maximumHeaderByteCount = 65_536
        static let maximumVertexCount = 100_000_000
        static let maximumASCIIVertexCount = 5_000_000
        static let maximumASCIIFileByteCount = 512 * 1_024 * 1_024

        let format: Format
        let vertexCount: Int
        let properties: [Property]
        let payloadOffset: Int
        let colorTypes: [PropertySemantic: ScalarType]

        var hasVertexColors: Bool {
            colorTypes[.red] != nil
                && colorTypes[.green] != nil
                && colorTypes[.blue] != nil
        }

        static func parse(data: Data) throws -> Header {
            var cursor = 0
            guard let firstLine = try PLYPointCloudLoader.nextLine(
                in: data,
                cursor: &cursor,
                maximumByteCount: maximumHeaderByteCount
            ), firstLine == "ply" else {
                throw PLYPointCloudError.invalidHeader
            }

            var format: Format?
            var vertexCount: Int?
            var properties: [Property] = []
            var vertexRecordByteCount = 0
            var colorTypes: [PropertySemantic: ScalarType] = [:]
            var currentElement: String?
            var dataElementPrecedesVertices = false
            var payloadOffset: Int?

            while cursor <= min(data.count, maximumHeaderByteCount) {
                guard let line = try PLYPointCloudLoader.nextLine(
                    in: data,
                    cursor: &cursor,
                    maximumByteCount: maximumHeaderByteCount
                ) else {
                    break
                }
                guard line.unicodeScalars.allSatisfy({ $0.value < 128 }) else {
                    throw PLYPointCloudError.invalidHeader
                }
                let tokens = line.split(whereSeparator: \Character.isWhitespace).map(String.init)
                guard let directive = tokens.first else { continue }

                switch directive {
                case "comment", "obj_info":
                    continue
                case "format":
                    guard format == nil, tokens.count == 3, tokens[2] == "1.0" else {
                        throw PLYPointCloudError.invalidHeader
                    }
                    switch tokens[1] {
                    case "ascii": format = .ascii
                    case "binary_little_endian": format = .binaryLittleEndian
                    case "binary_big_endian": format = .binaryBigEndian
                    default: throw PLYPointCloudError.unsupportedFormat
                    }
                case "element":
                    guard format != nil,
                          tokens.count == 3,
                          let count = Int(tokens[2]),
                          count >= 0 else {
                        throw PLYPointCloudError.invalidHeader
                    }
                    currentElement = tokens[1]
                    if tokens[1] == "vertex" {
                        guard vertexCount == nil, !dataElementPrecedesVertices else {
                            throw PLYPointCloudError.unsupportedVertexLayout
                        }
                        vertexCount = count
                    } else if vertexCount == nil, count > 0 {
                        dataElementPrecedesVertices = true
                    }
                case "property":
                    guard currentElement != nil else {
                        throw PLYPointCloudError.invalidHeader
                    }
                    guard currentElement == "vertex" else { continue }
                    guard tokens.count == 3,
                          tokens[1] != "list",
                          let scalarType = ScalarType(name: tokens[1]) else {
                        throw PLYPointCloudError.unsupportedVertexLayout
                    }
                    let semantic = PropertySemantic(name: tokens[2])
                    if semantic != .ignored {
                        guard !properties.contains(where: { $0.semantic == semantic }) else {
                            throw PLYPointCloudError.unsupportedVertexLayout
                        }
                        colorTypes[semantic] = scalarType
                    }
                    properties.append(
                        Property(
                            type: scalarType,
                            semantic: semantic,
                            byteOffset: vertexRecordByteCount
                        )
                    )
                    vertexRecordByteCount += scalarType.byteCount
                case "end_header":
                    guard tokens.count == 1 else {
                        throw PLYPointCloudError.invalidHeader
                    }
                    payloadOffset = cursor
                default:
                    throw PLYPointCloudError.invalidHeader
                }
                if payloadOffset != nil { break }
            }

            guard let format,
                  let vertexCount,
                  let payloadOffset,
                  payloadOffset <= maximumHeaderByteCount,
                  properties.contains(where: { $0.semantic == .x }),
                  properties.contains(where: { $0.semantic == .y }),
                  properties.contains(where: { $0.semantic == .z }) else {
                throw PLYPointCloudError.invalidHeader
            }
            let colorCount = [PropertySemantic.red, .green, .blue]
                .filter { colorTypes[$0] != nil }
                .count
            guard colorCount == 0 || colorCount == 3 else {
                throw PLYPointCloudError.unsupportedVertexLayout
            }
            return Header(
                format: format,
                vertexCount: vertexCount,
                properties: properties,
                payloadOffset: payloadOffset,
                colorTypes: colorTypes
            )
        }
    }

    struct VertexComponents {
        var x: Double?
        var y: Double?
        var z: Double?
        var red: Double?
        var green: Double?
        var blue: Double?
        var alpha: Double?

        mutating func assign(_ value: Double, semantic: PropertySemantic) {
            switch semantic {
            case .x: x = value
            case .y: y = value
            case .z: z = value
            case .red: red = value
            case .green: green = value
            case .blue: blue = value
            case .alpha: alpha = value
            case .ignored: break
            }
        }
    }

    struct SamplePlan {
        let sourceCount: Int
        let sampleCount: Int

        init(sourceCount: Int, maximumSampleCount: Int) {
            self.sourceCount = sourceCount
            sampleCount = min(sourceCount, maximumSampleCount)
        }

        func sourceIndex(forSampleNumber sampleNumber: Int) -> Int {
            guard sampleCount > 1 else { return 0 }
            return (sampleNumber * (sourceCount - 1)) / (sampleCount - 1)
        }
    }

    struct VertexAccumulator {
        private(set) var vertices: [PointCloudPreviewVertex]
        private(set) var bounds: PointCloudBounds?
        let hasVertexColors: Bool

        init(capacity: Int, hasVertexColors: Bool) {
            vertices = []
            vertices.reserveCapacity(capacity)
            self.hasVertexColors = hasVertexColors
        }

        mutating func include(
            _ components: VertexComponents,
            header: Header
        ) throws {
            guard let x = components.x,
                  let y = components.y,
                  let z = components.z else {
                throw PLYPointCloudError.invalidVertexData
            }
            let position = SIMD3<Float>(Float(x), Float(y), Float(z))
            guard position.x.isFinite, position.y.isFinite, position.z.isFinite else {
                throw PLYPointCloudError.invalidVertexData
            }

            if let existing = bounds {
                bounds = PointCloudBounds(
                    minimum: SIMD3(
                        min(existing.minimum.x, position.x),
                        min(existing.minimum.y, position.y),
                        min(existing.minimum.z, position.z)
                    ),
                    maximum: SIMD3(
                        max(existing.maximum.x, position.x),
                        max(existing.maximum.y, position.y),
                        max(existing.maximum.z, position.z)
                    )
                )
            } else {
                bounds = PointCloudBounds(minimum: position, maximum: position)
            }

            let color: SIMD4<Float>
            if hasVertexColors {
                guard let red = components.red,
                      let green = components.green,
                      let blue = components.blue,
                      let redType = header.colorTypes[.red],
                      let greenType = header.colorTypes[.green],
                      let blueType = header.colorTypes[.blue],
                      let normalizedRed = redType.normalizedColor(red),
                      let normalizedGreen = greenType.normalizedColor(green),
                      let normalizedBlue = blueType.normalizedColor(blue) else {
                    throw PLYPointCloudError.invalidVertexData
                }
                let normalizedAlpha: Float
                if let alpha = components.alpha,
                   let alphaType = header.colorTypes[.alpha],
                   let value = alphaType.normalizedColor(alpha) {
                    normalizedAlpha = value
                } else {
                    normalizedAlpha = 1
                }
                color = SIMD4(
                    normalizedRed,
                    normalizedGreen,
                    normalizedBlue,
                    normalizedAlpha
                )
            } else {
                color = SIMD4(0.30, 0.72, 1.0, 1.0)
            }
            vertices.append(PointCloudPreviewVertex(position: position, color: color))
        }
    }
}
