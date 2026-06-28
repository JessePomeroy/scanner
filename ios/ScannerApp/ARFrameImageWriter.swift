import ARKit
import CoreImage
import Foundation
import UIKit

enum ARFrameImageWriterError: Error {
    case jpegEncodingFailed
}

struct SavedFrameImage {
    let url: URL
    let width: Int
    let height: Int
}

struct ARFrameImageWriter {
    private let context = CIContext()
    private let colorSpace = CGColorSpaceCreateDeviceRGB()

    func estimateSharpnessScore(from pixelBuffer: CVPixelBuffer) -> Float {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer {
            CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly)
        }

        let plane = CVPixelBufferIsPlanar(pixelBuffer) ? 0 : nil
        let width = plane.map { CVPixelBufferGetWidthOfPlane(pixelBuffer, $0) } ?? CVPixelBufferGetWidth(pixelBuffer)
        let height = plane.map { CVPixelBufferGetHeightOfPlane(pixelBuffer, $0) } ?? CVPixelBufferGetHeight(pixelBuffer)
        let bytesPerRow = plane.map { CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, $0) } ?? CVPixelBufferGetBytesPerRow(pixelBuffer)
        let baseAddress = plane.flatMap { CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, $0) }
            ?? CVPixelBufferGetBaseAddress(pixelBuffer)

        guard let baseAddress,
              width > 16,
              height > 16 else {
            return 0
        }

        let pixels = baseAddress.assumingMemoryBound(to: UInt8.self)
        let step = max(4, min(width, height) / 96)
        var gradientTotal: Float = 0
        var sampleCount: Float = 0

        var y = step
        while y < height - step {
            var x = step
            while x < width - step {
                let centerIndex = y * bytesPerRow + x
                let rightIndex = centerIndex + step
                let downIndex = centerIndex + step * bytesPerRow
                let horizontal = abs(Int(pixels[centerIndex]) - Int(pixels[rightIndex]))
                let vertical = abs(Int(pixels[centerIndex]) - Int(pixels[downIndex]))

                gradientTotal += Float(horizontal + vertical) * 0.5
                sampleCount += 1
                x += step
            }
            y += step
        }

        guard sampleCount > 0 else { return 0 }

        let averageGradient = gradientTotal / sampleCount
        return min(1, max(0, averageGradient / 24))
    }

    func writeJPEG(from pixelBuffer: CVPixelBuffer, to url: URL, quality: CGFloat = 0.92) throws -> SavedFrameImage {
        let image = CIImage(cvPixelBuffer: pixelBuffer)
            .oriented(.right)
        let extent = image.extent.integral

        guard let data = context.jpegRepresentation(
            of: image,
            colorSpace: colorSpace,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: quality]
        ) else {
            throw ARFrameImageWriterError.jpegEncodingFailed
        }

        try data.write(to: url, options: [.atomic])

        return SavedFrameImage(
            url: url,
            width: Int(extent.width),
            height: Int(extent.height)
        )
    }
}
