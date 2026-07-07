import AVFoundation
import Foundation
import ImageIO
import UIKit

enum CameraCaptureError: Error {
    case cameraUnavailable
    case outputUnavailable
    case photoDataUnavailable
    case imageDecodeFailed
}

struct HighResolutionPhotoMetadata {
    let width: Int
    let height: Int
    let exposureDuration: Double?
    let iso: Float?
}

struct SavedHighResolutionPhoto {
    let image: SavedFrameImage
    let metadata: HighResolutionPhotoMetadata
}

/// Handles high-resolution still photo capture.
///
/// This file will wrap AVFoundation setup, focus/exposure/white-balance locking,
/// and saving accepted keyframe images into the scan package.
final class CameraCaptureManager: NSObject {
    private let captureSession = AVCaptureSession()
    private let photoOutput = AVCapturePhotoOutput()

    private(set) var isPrepared = false
    private var retainedDelegates: [UUID: PhotoCaptureDelegate] = [:]
    private let delegateQueue = DispatchQueue(label: "ScannerApp.CameraCaptureManager.delegates")

    func prepareCamera() throws {
        guard !isPrepared else { return }

        captureSession.beginConfiguration()
        captureSession.sessionPreset = .photo
        defer { captureSession.commitConfiguration() }

        guard let camera = AVCaptureDevice.default(
            .builtInWideAngleCamera,
            for: .video,
            position: .back
        ) else {
            throw CameraCaptureError.cameraUnavailable
        }

        let input = try AVCaptureDeviceInput(device: camera)
        guard captureSession.canAddInput(input),
              captureSession.canAddOutput(photoOutput) else {
            throw CameraCaptureError.outputUnavailable
        }

        captureSession.addInput(input)
        captureSession.addOutput(photoOutput)
        photoOutput.maxPhotoQualityPrioritization = .quality

        try lockStableCameraSettings(on: camera)
        isPrepared = true
    }

    func startRunning() {
        guard isPrepared, !captureSession.isRunning else { return }
        captureSession.startRunning()
    }

    func stopRunning() {
        guard captureSession.isRunning else { return }
        captureSession.stopRunning()
    }

    func capturePhoto(delegate: AVCapturePhotoCaptureDelegate) {
        let settings = AVCapturePhotoSettings()
        settings.photoQualityPrioritization = .quality
        photoOutput.capturePhoto(with: settings, delegate: delegate)
    }

    func capturePhoto(to url: URL, completion: @escaping (Result<SavedHighResolutionPhoto, Error>) -> Void) {
        let identifier = UUID()
        let delegate = PhotoCaptureDelegate(destinationURL: url) { [weak self] result in
            if let self {
                self.delegateQueue.sync {
                    _ = self.retainedDelegates.removeValue(forKey: identifier)
                }
            }
            completion(result)
        }

        delegateQueue.sync {
            retainedDelegates[identifier] = delegate
        }

        capturePhoto(delegate: delegate)
    }

    private func lockStableCameraSettings(on camera: AVCaptureDevice) throws {
        try camera.lockForConfiguration()
        defer { camera.unlockForConfiguration() }

        if camera.isFocusModeSupported(.continuousAutoFocus) {
            camera.focusMode = .continuousAutoFocus
        }

        if camera.isExposureModeSupported(.continuousAutoExposure) {
            camera.exposureMode = .continuousAutoExposure
        }

        if camera.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
            camera.whiteBalanceMode = .continuousAutoWhiteBalance
        }
    }
}

private final class PhotoCaptureDelegate: NSObject, AVCapturePhotoCaptureDelegate {
    private let destinationURL: URL
    private let completion: (Result<SavedHighResolutionPhoto, Error>) -> Void

    init(
        destinationURL: URL,
        completion: @escaping (Result<SavedHighResolutionPhoto, Error>) -> Void
    ) {
        self.destinationURL = destinationURL
        self.completion = completion
        super.init()
    }

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishProcessingPhoto photo: AVCapturePhoto,
        error: Error?
    ) {
        if let error {
            completion(.failure(error))
            return
        }

        guard let data = photo.fileDataRepresentation() else {
            completion(.failure(CameraCaptureError.photoDataUnavailable))
            return
        }

        do {
            try data.write(to: destinationURL, options: [.atomic])
            let metadata = try photoMetadata(from: data, photo: photo)
            completion(
                .success(
                    SavedHighResolutionPhoto(
                        image: SavedFrameImage(
                            url: destinationURL,
                            width: metadata.width,
                            height: metadata.height
                        ),
                        metadata: metadata
                    )
                )
            )
        } catch {
            completion(.failure(error))
        }
    }

    private func photoMetadata(from data: Data, photo: AVCapturePhoto) throws -> HighResolutionPhotoMetadata {
        guard let image = UIImage(data: data) else {
            throw CameraCaptureError.imageDecodeFailed
        }

        let exif = photo.metadata[kCGImagePropertyExifDictionary as String] as? [String: Any]
        let exposureDuration = exif?[kCGImagePropertyExifExposureTime as String] as? Double
        let isoValues = exif?[kCGImagePropertyExifISOSpeedRatings as String] as? [NSNumber]

        return HighResolutionPhotoMetadata(
            width: Int(image.size.width * image.scale),
            height: Int(image.size.height * image.scale),
            exposureDuration: exposureDuration,
            iso: isoValues?.first?.floatValue
        )
    }
}
