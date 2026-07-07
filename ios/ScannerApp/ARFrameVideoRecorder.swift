import ARKit
import AVFoundation
import CoreVideo
import Foundation

enum ARFrameVideoRecorderError: Error {
    case writerUnavailable
    case inputUnavailable
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
    private var didReachDurationLimit = false
    private(set) var isRecording = false

    var durationSeconds: Double? {
        guard let lastPresentationTime else { return nil }
        let seconds = CMTimeGetSeconds(lastPresentationTime)
        return seconds.isFinite ? seconds : nil
    }

    var reachedDurationLimit: Bool {
        didReachDurationLimit
    }

    func start(outputURL: URL, relativePath: String, capturedAt: String, firstFrame: ARFrame) throws {
        cancel()
        self.outputURL = outputURL

        if FileManager.default.fileExists(atPath: outputURL.path) {
            try FileManager.default.removeItem(at: outputURL)
        }

        let pixelBuffer = firstFrame.capturedImage
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let writer: AVAssetWriter
        do {
            writer = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
        } catch {
            cancel()
            throw error
        }
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
            cancel()
            throw ARFrameVideoRecorderError.inputUnavailable
        }

        writer.add(input)
        guard writer.startWriting() else {
            let error = writer.error ?? ARFrameVideoRecorderError.writerUnavailable
            cancel()
            throw error
        }

        let startTime = CMTime(seconds: 0, preferredTimescale: 600)
        writer.startSession(atSourceTime: startTime)

        self.writer = writer
        self.input = input
        self.adaptor = adaptor
        self.relativePath = relativePath
        self.capturedAt = capturedAt
        self.startTimestamp = firstFrame.timestamp
        self.width = width
        self.height = height
        isRecording = true
    }

    func append(_ frame: ARFrame, maximumDurationSeconds: Double? = nil) {
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

        if let maximumDurationSeconds,
           CMTimeGetSeconds(presentationTime) > maximumDurationSeconds {
            didReachDurationLimit = true
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
        let finished = semaphore.wait(timeout: .now() + 5) == .success

        let duration = CMTimeGetSeconds(lastPresentationTime ?? .zero)
        guard finished,
              writer.status == .completed,
              FileManager.default.fileExists(atPath: outputURL.path),
              duration.isFinite,
              duration > 0 else {
            cancel()
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

        resetState(removeOutputFile: false)
        return metadata
    }

    func cancel() {
        input?.markAsFinished()
        writer?.cancelWriting()
        resetState(removeOutputFile: true)
    }

    private func resetState(removeOutputFile: Bool = false) {
        if removeOutputFile,
           let outputURL,
           FileManager.default.fileExists(atPath: outputURL.path) {
            try? FileManager.default.removeItem(at: outputURL)
        }

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
        didReachDurationLimit = false
    }
}
