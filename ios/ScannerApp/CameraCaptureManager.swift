import AVFoundation
import Foundation

enum CameraCaptureError: Error {
    case cameraUnavailable
    case outputUnavailable
}

/// Handles high-resolution still photo capture.
///
/// This file will wrap AVFoundation setup, focus/exposure/white-balance locking,
/// and saving accepted keyframe images into the scan package.
final class CameraCaptureManager: NSObject {
    private let captureSession = AVCaptureSession()
    private let photoOutput = AVCapturePhotoOutput()

    private(set) var isPrepared = false

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
