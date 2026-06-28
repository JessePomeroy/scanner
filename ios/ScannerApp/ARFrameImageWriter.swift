import ARKit
import CoreImage
import Foundation
import UIKit

enum ARFrameImageWriterError: Error {
    case jpegEncodingFailed
}

struct ARFrameImageWriter {
    private let context = CIContext()
    private let colorSpace = CGColorSpaceCreateDeviceRGB()

    func writeJPEG(from pixelBuffer: CVPixelBuffer, to url: URL, quality: CGFloat = 0.92) throws {
        let image = CIImage(cvPixelBuffer: pixelBuffer)
            .oriented(.right)

        guard let data = context.jpegRepresentation(
            of: image,
            colorSpace: colorSpace,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: quality]
        ) else {
            throw ARFrameImageWriterError.jpegEncodingFailed
        }

        try data.write(to: url, options: [.atomic])
    }
}
