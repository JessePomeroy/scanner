import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct NormalizedMaskPoint: Equatable {
    let x: Double
    let y: Double
}

enum CaptureMaskRasterizerError: Error, Equatable {
    case invalidDimensions
    case insufficientPoints
    case nonFinitePoint
    case pointOutOfBounds
    case degeneratePolygon
    case imageCreationFailed
    case pngEncodingFailed
}

/// Converts a normalized keep polygon into an 8-bit grayscale PNG.
/// White pixels are reconstructed; black pixels are excluded.
struct CaptureMaskRasterizer {
    func pngData(
        for polygon: [NormalizedMaskPoint],
        width: Int,
        height: Int
    ) throws -> Data {
        guard width > 0, height > 0 else {
            throw CaptureMaskRasterizerError.invalidDimensions
        }
        try validate(polygon)

        let (pixelCount, overflow) = width.multipliedReportingOverflow(by: height)
        guard !overflow else {
            throw CaptureMaskRasterizerError.invalidDimensions
        }
        var pixels = [UInt8](repeating: 0, count: pixelCount)
        let colorSpace = CGColorSpaceCreateDeviceGray()
        let image = pixels.withUnsafeMutableBytes { pixelBytes -> CGImage? in
            guard let context = CGContext(
                data: pixelBytes.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.none.rawValue
            ) else {
                return nil
            }

            context.setShouldAntialias(false)
            context.setAllowsAntialiasing(false)
            context.translateBy(x: 0, y: CGFloat(height))
            context.scaleBy(x: 1, y: -1)
            context.setFillColor(gray: 1, alpha: 1)
            context.beginPath()
            context.move(to: pixelPoint(polygon[0], width: width, height: height))
            for point in polygon.dropFirst() {
                context.addLine(to: pixelPoint(point, width: width, height: height))
            }
            context.closePath()
            context.fillPath(using: .winding)
            return context.makeImage()
        }
        guard let image else {
            throw CaptureMaskRasterizerError.imageCreationFailed
        }

        return try withExtendedLifetime(pixels) {
            let output = NSMutableData()
            guard let destination = CGImageDestinationCreateWithData(
                output,
                UTType.png.identifier as CFString,
                1,
                nil
            ) else {
                throw CaptureMaskRasterizerError.pngEncodingFailed
            }
            CGImageDestinationAddImage(destination, image, nil)
            guard CGImageDestinationFinalize(destination) else {
                throw CaptureMaskRasterizerError.pngEncodingFailed
            }
            return output as Data
        }
    }

    func validate(_ polygon: [NormalizedMaskPoint]) throws {
        guard polygon.count >= 3 else {
            throw CaptureMaskRasterizerError.insufficientPoints
        }
        for point in polygon {
            guard point.x.isFinite, point.y.isFinite else {
                throw CaptureMaskRasterizerError.nonFinitePoint
            }
            guard (0...1).contains(point.x), (0...1).contains(point.y) else {
                throw CaptureMaskRasterizerError.pointOutOfBounds
            }
        }

        let signedDoubleArea = polygon.indices.reduce(0.0) { area, index in
            let next = polygon[(index + 1) % polygon.count]
            return area + polygon[index].x * next.y - next.x * polygon[index].y
        }
        guard abs(signedDoubleArea) > Double.ulpOfOne * Double(polygon.count) else {
            throw CaptureMaskRasterizerError.degeneratePolygon
        }
    }

    private func pixelPoint(
        _ point: NormalizedMaskPoint,
        width: Int,
        height: Int
    ) -> CGPoint {
        CGPoint(x: point.x * Double(width), y: point.y * Double(height))
    }
}
