import ARKit
import SceneKit
import SwiftUI

struct ARSessionView: UIViewRepresentable {
    let session: ARSession
    var onWorldTap: ((SIMD3<Float>) -> Void)?

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.session = session
        view.automaticallyUpdatesLighting = true
        view.scene = SCNScene()
        view.addGestureRecognizer(
            UITapGestureRecognizer(
                target: context.coordinator,
                action: #selector(Coordinator.handleTap(_:))
            )
        )
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        if uiView.session !== session {
            uiView.session = session
        }
        context.coordinator.onWorldTap = onWorldTap
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(onWorldTap: onWorldTap)
    }

    final class Coordinator: NSObject {
        var onWorldTap: ((SIMD3<Float>) -> Void)?

        init(onWorldTap: ((SIMD3<Float>) -> Void)?) {
            self.onWorldTap = onWorldTap
        }

        @objc func handleTap(_ recognizer: UITapGestureRecognizer) {
            guard let view = recognizer.view as? ARSCNView else { return }

            let point = recognizer.location(in: view)

            guard let query = view.raycastQuery(
                from: point,
                allowing: .estimatedPlane,
                alignment: .any
            ) else {
                return
            }

            if let result = view.session.raycast(query).first {
                onWorldTap?(result.worldTransform.translation)
            }
        }
    }
}
