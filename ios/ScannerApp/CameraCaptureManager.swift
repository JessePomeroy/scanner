import ARKit
import Foundation

enum KeyframeImageSource: String {
    case arkitHighResolution = "arkit_high_resolution"
    case arkitVideoFrameFallback = "arkit_video_frame_fallback"
}

struct CapturedKeyframe {
    let frame: ARFrame
    let source: KeyframeImageSource
    let highResolutionFailure: String?
}

/// Requests pose-synchronized, out-of-band high-resolution frames from the
/// ARKit session. ARKit keeps its normal tracking stream running during this
/// capture and populates the returned frame's pose and imaging parameters.
final class CameraCaptureManager {
    func supportsHighResolutionCapture(in session: ARSession) -> Bool {
        session.configuration?.videoFormat.isRecommendedForHighResolutionFrameCapturing == true
    }

    func captureKeyframe(
        from session: ARSession,
        fallbackFrame: ARFrame,
        completionQueue: DispatchQueue,
        completion: @escaping (CapturedKeyframe) -> Void
    ) {
        guard supportsHighResolutionCapture(in: session) else {
            completion(
                CapturedKeyframe(
                    frame: fallbackFrame,
                    source: .arkitVideoFrameFallback,
                    highResolutionFailure: "unsupported_video_format"
                )
            )
            return
        }

        session.captureHighResolutionFrame { frame, error in
            let result: CapturedKeyframe
            if let frame {
                result = CapturedKeyframe(
                    frame: frame,
                    source: .arkitHighResolution,
                    highResolutionFailure: nil
                )
            } else {
                result = CapturedKeyframe(
                    frame: fallbackFrame,
                    source: .arkitVideoFrameFallback,
                    highResolutionFailure: error.map(Self.failureCode)
                        ?? "high_resolution_frame_unavailable"
                )
            }
            completionQueue.async {
                completion(result)
            }
        }
    }

    private static func failureCode(_ error: Error) -> String {
        let value = error as NSError
        return "arkit_error_\(value.code)"
    }
}
