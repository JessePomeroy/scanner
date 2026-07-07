import ARKit
import AVFoundation
import CoreVideo
import Foundation

enum ARFrameVideoRecorderError: Error {
    case writerUnavailable
    case inputUnavailable
    case adaptorUnavailable
    case missingVideoTrack
}

/// Encodes the live ARFrame camera stream into a scan-package video file.
final class ARFrameVideoRecorder {
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var outputURL: URL?
    private var relativePath: String?
    private var capturedAt: String?
    private var startTimestamp: TimeInterval?
    private var lastPresentationTime: CMTime?
    private var frameCount = 0
    private var width = 0
    private var height = 0
    private(set) var isRecording = false

    func start(outputURL: URL, relativePath: String, capturedAt: String, firstFrame: ARFrame) throws {
        resetState()

        if FileManager.default.fileExists(atPath: outputURL.path) {
            try FileManager.default.removeItem(at: outputURL)
        }

        let pixelBuffer = firstFrame.capturedImage
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let writer = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
        let outputSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height
        ]
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: outputSettings)
        input.expectsMediaDataInRealTime = true
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: input,
            sourcePixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: CVPixelBufferGetPixelFormatType(pixelBuffer),
                kCVPixelBufferWidthKey as String: width,
                kCVPixelBufferHeightKey as String: height
            ]
        )

        guard writer.canAdd(input) else {
            throw ARFrameVideoRecorderError.inputUnavailable
        }

        writer.add(input)
        guard writer.startWriting() else {
            throw writer.error ?? ARFrameVideoRecorderError.writerUnavailable
        }

        let startTime = CMTime(seconds: 0, preferredTimescale: 600)
        writer.startSession(atSourceTime: startTime)

        self.writer = writer
        self.input = input
        self.adaptor = adaptor
        self.outputURL = outputURL
        self.relativePath = relativePath
        self.capturedAt = capturedAt
        self.startTimestamp = firstFrame.timestamp
        self.width = width
        self.height = height
        isRecording = true
    }

    func append(_ frame: ARFrame) {
        guard isRecording,
              let writer,
              writer.status == .writing,
              let input,
              input.isReadyForMoreMediaData,
              let adaptor,
              let startTimestamp else {
            return
        }

        let presentationTime = CMTime(
            seconds: frame.timestamp - startTimestamp,
            preferredTimescale: 600
        )
        if let lastPresentationTime,
           presentationTime <= lastPresentationTime {
            return
        }

        if adaptor.append(frame.capturedImage, withPresentationTime: presentationTime) {
            frameCount += 1
            lastPresentationTime = presentationTime
        }
    }

    func finish() -> VideoCaptureMetadata? {
        guard isRecording,
              let writer,
              let input,
              let relativePath,
              let capturedAt,
              let outputURL else {
            resetState()
            return nil
        }

        input.markAsFinished()
        let semaphore = DispatchSemaphore(value: 0)
        writer.finishWriting {
            semaphore.signal()
        }
        _ = semaphore.wait(timeout: .now() + 5)

        let duration = CMTimeGetSeconds(lastPresentationTime ?? .zero)
        guard writer.status == .completed,
              FileManager.default.fileExists(atPath: outputURL.path),
              duration.isFinite,
              duration > 0 else {
            resetState()
            return nil
        }

        let metadata = VideoCaptureMetadata(
            path: relativePath,
            capturedAt: capturedAt,
            durationSeconds: duration,
            frameRate: duration > 0 ? Double(frameCount) / duration : nil,
            resolution: [width, height],
            codec: "h264",
            includesAudio: false
        )

        resetState()
        return metadata
    }

    func cancel() {
        input?.markAsFinished()
        writer?.cancelWriting()

        if let outputURL,
           FileManager.default.fileExists(atPath: outputURL.path) {
            try? FileManager.default.removeItem(at: outputURL)
        }

        resetState()
    }

    private func resetState() {
        writer = nil
        input = nil
        adaptor = nil
        outputURL = nil
        relativePath = nil
        capturedAt = nil
        startTimestamp = nil
        lastPresentationTime = nil
        frameCount = 0
        width = 0
        height = 0
        isRecording = false
    }
}
