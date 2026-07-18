import CoreGraphics
import Foundation

@main
struct VerifyCaptureMaskMapper {
    static func main() throws {
        let mapper = CaptureMaskCoordinateMapper()
        let polygon = [
            NormalizedMaskPoint(x: 0, y: 0),
            NormalizedMaskPoint(x: 1, y: 0),
            NormalizedMaskPoint(x: 1, y: 1)
        ]

        let identity = try mapper.imagePolygon(
            from: polygon,
            previewSize: CGSize(width: 300, height: 400),
            imageWidth: 1200,
            imageHeight: 1600
        )
        assertClose(identity[0].x, 0)
        assertClose(identity[0].y, 0)
        assertClose(identity[2].x, 1)
        assertClose(identity[2].y, 1)

        let tallPreview = try mapper.imagePolygon(
            from: polygon,
            previewSize: CGSize(width: 390, height: 844),
            imageWidth: 1440,
            imageHeight: 1920
        )
        assert(tallPreview[0].x > 0)
        assert(tallPreview[1].x < 1)
        assertClose(tallPreview[0].y, 0)
        assertClose(tallPreview[2].y, 1)

        do {
            _ = try mapper.imagePolygon(
                from: polygon,
                previewSize: .zero,
                imageWidth: 1440,
                imageHeight: 1920
            )
            assertionFailure("Expected invalid dimensions to fail")
        } catch CaptureMaskCoordinateMapperError.invalidDimensions {
            // Expected.
        }

        print("Verified capture mask coordinate mapping")
    }

    private static func assertClose(
        _ actual: Double,
        _ expected: Double,
        tolerance: Double = 0.000_001
    ) {
        assert(abs(actual - expected) <= tolerance, "\(actual) != \(expected)")
    }
}
