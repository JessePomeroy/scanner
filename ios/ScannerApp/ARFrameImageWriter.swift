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
