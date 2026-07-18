import CoreGraphics
import Foundation

enum CaptureMaskCoordinateMapperError: Error, Equatable {
    case invalidDimensions
}

/// Maps normalized points from an aspect-filled portrait preview into the
/// normalized coordinates of the portrait JPEG displayed by that preview.
struct CaptureMaskCoordinateMapper {
    func imagePolygon(
        from previewPolygon: [NormalizedMaskPoint],
        previewSize: CGSize,
        imageWidth: Int,
        imageHeight: Int
    ) throws -> [NormalizedMaskPoint] {
        guard previewSize.width > 0,
              previewSize.height > 0,
              imageWidth > 0,
              imageHeight > 0 else {
            throw CaptureMaskCoordinateMapperError.invalidDimensions
        }

        let imageSize = CGSize(width: imageWidth, height: imageHeight)
        let scale = max(
            previewSize.width / imageSize.width,
            previewSize.height / imageSize.height
        )
        let displayedSize = CGSize(
            width: imageSize.width * scale,
            height: imageSize.height * scale
        )
        let cropX = (displayedSize.width - previewSize.width) / 2
        let cropY = (displayedSize.height - previewSize.height) / 2

        return previewPolygon.map { point in
            NormalizedMaskPoint(
                x: Double((CGFloat(point.x) * previewSize.width + cropX) / displayedSize.width),
                y: Double((CGFloat(point.y) * previewSize.height + cropY) / displayedSize.height)
            )
        }
    }
}
