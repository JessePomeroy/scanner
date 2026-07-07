import AVFoundation
import Foundation
import ImageIO

enum CameraCaptureError: Error {
    case cameraUnavailable
    case cameraNotPrepared
    case captureSessionNotRunning
    case outputUnavailable
    case photoDataUnavailable
    case imageMetadataUnavailable
    case unsupportedPhotoDimensions
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
    private var selectedMaxPhotoDimensions: CMVideoDimensions?
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
        selectedMaxPhotoDimensions = largestPhotoDimensions(for: camera)
        if let selectedMaxPhotoDimensions {
            photoOutput.maxPhotoDimensions = selectedMaxPhotoDimensions
        }

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
        if let selectedMaxPhotoDimensions {
            settings.maxPhotoDimensions = selectedMaxPhotoDimensions
        }
        photoOutput.capturePhoto(with: settings, delegate: delegate)
    }

    func capturePhoto(
        to url: URL,
        completionQueue: DispatchQueue = .main,
        completion: @escaping (Result<SavedHighResolutionPhoto, Error>) -> Void
    ) {
        guard isPrepared else {
            completionQueue.async {
                completion(.failure(CameraCaptureError.cameraNotPrepared))
            }
            return
        }
        guard captureSession.isRunning else {
            completionQueue.async {
                completion(.failure(CameraCaptureError.captureSessionNotRunning))
            }
            return
        }
        guard selectedMaxPhotoDimensions != nil else {
            completionQueue.async {
                completion(.failure(CameraCaptureError.unsupportedPhotoDimensions))
            }
            return
        }

        let identifier = UUID()
        let delegate = PhotoCaptureDelegate(
            destinationURL: url,
            completionQueue: completionQueue,
            completion: completion,
            onFinish: { [weak self] in
                if let self {
                    self.delegateQueue.sync {
                        _ = self.retainedDelegates.removeValue(forKey: identifier)
                    }
                }
            }
        )

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

    private func largestPhotoDimensions(for camera: AVCaptureDevice) -> CMVideoDimensions? {
        camera.activeFormat.supportedMaxPhotoDimensions.max { left, right in
            Int(left.width) * Int(left.height) < Int(right.width) * Int(right.height)
        }
    }
}

private final class PhotoCaptureDelegate: NSObject, AVCapturePhotoCaptureDelegate {
    private let destinationURL: URL
    private let completionQueue: DispatchQueue
    private let completion: (Result<SavedHighResolutionPhoto, Error>) -> Void
    private let onFinish: () -> Void
    private var processingResult: Result<SavedHighResolutionPhoto, Error>?

    init(
        destinationURL: URL,
        completionQueue: DispatchQueue,
        completion: @escaping (Result<SavedHighResolutionPhoto, Error>) -> Void,
        onFinish: @escaping () -> Void
    ) {
        self.destinationURL = destinationURL
        self.completionQueue = completionQueue
        self.completion = completion
        self.onFinish = onFinish
        super.init()
    }

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishProcessingPhoto photo: AVCapturePhoto,
        error: Error?
    ) {
        if let error {
            processingResult = .failure(error)
            return
        }

        guard let data = photo.fileDataRepresentation() else {
            processingResult = .failure(CameraCaptureError.photoDataUnavailable)
            return
        }

        do {
            let metadata = try photoMetadata(from: data, photo: photo)
            try data.write(to: destinationURL, options: [.atomic])
            processingResult = .success(
                SavedHighResolutionPhoto(
                    image: SavedFrameImage(
                        url: destinationURL,
                        width: metadata.width,
                        height: metadata.height
                    ),
                    metadata: metadata
                )
            )
        } catch {
            processingResult = .failure(error)
        }
    }

    private func photoMetadata(from data: Data, photo: AVCapturePhoto) throws -> HighResolutionPhotoMetadata {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any],
              let width = properties[kCGImagePropertyPixelWidth as String] as? Int,
              let height = properties[kCGImagePropertyPixelHeight as String] as? Int else {
            throw CameraCaptureError.imageMetadataUnavailable
        }

        let exif = photo.metadata[kCGImagePropertyExifDictionary as String] as? [String: Any]
        let exposureDuration = exif?[kCGImagePropertyExifExposureTime as String] as? Double
        let isoValues = exif?[kCGImagePropertyExifISOSpeedRatings as String] as? [NSNumber]

        return HighResolutionPhotoMetadata(
            width: width,
            height: height,
            exposureDuration: exposureDuration,
            iso: isoValues?.first?.floatValue
        )
    }

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishCaptureFor resolvedSettings: AVCaptureResolvedPhotoSettings,
        error: Error?
    ) {
        let finalResult = error.map { Result<SavedHighResolutionPhoto, Error>.failure($0) }
            ?? processingResult
            ?? .failure(CameraCaptureError.photoDataUnavailable)

        completionQueue.async {
            self.completion(finalResult)
            self.onFinish()
        }
    }
}
