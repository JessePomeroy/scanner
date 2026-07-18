import CoreGraphics
import Foundation
import ImageIO

private enum VerificationError: Error {
    case failed(String)
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    guard condition() else { throw VerificationError.failed(message) }
}

@main
private enum MaskAuthoringContractVerifier {
    static func main() throws {
        let keep = MaskAuthoringRegion(
            operation: .keep,
            points: [
                .init(x: 0.1, y: 0.1), .init(x: 0.9, y: 0.1),
                .init(x: 0.9, y: 0.9), .init(x: 0.1, y: 0.9),
            ]
        )
        let erase = MaskAuthoringRegion(
            operation: .erase,
            points: [
                .init(x: 0.4, y: 0.4), .init(x: 0.6, y: 0.4),
                .init(x: 0.6, y: 0.6), .init(x: 0.4, y: 0.6),
            ]
        )
        let rasterizer = CaptureMaskRasterizer()
        let png = try rasterizer.pngData(for: [keep, erase], width: 100, height: 100)
        let pixels = try decodedPixels(png, width: 100, height: 100)
        try require(pixels[20 * 100 + 20] == 255, "Keep region was not white")
        try require(pixels[50 * 100 + 50] == 0, "Erase region did not override keep")
        try require(pixels[95 * 100 + 95] == 0, "Outside region was not black")

        let plan = MaskAuthoringPlan(
            schemaVersion: "1.0",
            authoringMode: "representative_frames",
            coordinateSpace: "normalized_capture_image",
            maskConvention: "white_keep_black_exclude",
            revision: 1,
            representativeFrames: [
                .init(frameID: 7, image: "images/frame.jpg", regions: [keep, erase])
            ]
        )
        let encoded = try JSONEncoder().encode(plan)
        let decoded = try JSONDecoder().decode(MaskAuthoringPlan.self, from: encoded)
        try require(decoded == plan, "Mask-authoring JSON did not round-trip")
        let object = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        try require(object?["coordinate_space"] as? String == "normalized_capture_image", "Wrong JSON keys")
        try require(
            MaskAuthoringSampleSelector.representativeIndices(frameCount: 10) == [0, 2, 4, 7, 9],
            "Representative frame selection diverged from the backend contract"
        )

        do {
            _ = try rasterizer.pngData(for: [erase], width: 100, height: 100)
            throw VerificationError.failed("Erase-only authoring was accepted")
        } catch CaptureMaskRasterizerError.invalidRegionSet {
            // Expected.
        }
        print("Mask authoring contract verification passed")
    }

    private static func decodedPixels(_ png: Data, width: Int, height: Int) throws -> [UInt8] {
        guard let source = CGImageSourceCreateWithData(png as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw VerificationError.failed("Could not decode generated PNG")
        }
        var pixels = [UInt8](repeating: 0, count: width * height)
        let rendered = pixels.withUnsafeMutableBytes { bytes -> Bool in
            guard let context = CGContext(
                data: bytes.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width,
                space: CGColorSpaceCreateDeviceGray(),
                bitmapInfo: CGImageAlphaInfo.none.rawValue
            ) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard rendered else {
            throw VerificationError.failed("Could not render generated PNG")
        }
        return pixels
    }
}
