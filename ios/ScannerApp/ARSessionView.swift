import ARKit
import SceneKit
import SwiftUI
import UIKit
import simd

struct ARSessionView: UIViewRepresentable {
    let session: ARSession
    var cameraPath: [SIMD3<Float>] = []
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
        context.coordinator.updateCameraPath(cameraPath, in: uiView)
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(onWorldTap: onWorldTap)
    }

    final class Coordinator: NSObject {
        var onWorldTap: ((SIMD3<Float>) -> Void)?
        private let pathRoot = SCNNode()
        private var renderedPath: [SIMD3<Float>] = []

        init(onWorldTap: ((SIMD3<Float>) -> Void)?) {
            self.onWorldTap = onWorldTap
        }

        func updateCameraPath(_ points: [SIMD3<Float>], in view: ARSCNView) {
            if pathRoot.parent == nil {
                pathRoot.name = "scene-camera-path"
                view.scene.rootNode.addChildNode(pathRoot)
            }
            guard points != renderedPath else { return }
            guard points.count >= renderedPath.count,
                  Array(points.prefix(renderedPath.count)) == renderedPath else {
                pathRoot.childNodes.forEach { $0.removeFromParentNode() }
                renderedPath.removeAll(keepingCapacity: true)
                appendPath(points)
                return
            }
            appendPath(Array(points.dropFirst(renderedPath.count)))
        }

        private func appendPath(_ newPoints: [SIMD3<Float>]) {
            for point in newPoints {
                if let previous = renderedPath.last {
                    pathRoot.addChildNode(pathSegment(from: previous, to: point))
                }
                pathRoot.addChildNode(pathMarker(at: point))
                renderedPath.append(point)
            }
        }

        private func pathMarker(at point: SIMD3<Float>) -> SCNNode {
            let sphere = SCNSphere(radius: 0.018)
            sphere.segmentCount = 8
            sphere.firstMaterial?.diffuse.contents = UIColor.systemCyan
            sphere.firstMaterial?.emission.contents = UIColor.systemCyan.withAlphaComponent(0.35)
            let node = SCNNode(geometry: sphere)
            node.simdPosition = point
            return node
        }

        private func pathSegment(from start: SIMD3<Float>, to end: SIMD3<Float>) -> SCNNode {
            let delta = end - start
            let length = max(0.001, simd_length(delta))
            let cylinder = SCNCylinder(radius: 0.006, height: CGFloat(length))
            cylinder.radialSegmentCount = 6
            cylinder.firstMaterial?.diffuse.contents = UIColor.systemCyan.withAlphaComponent(0.7)
            let node = SCNNode(geometry: cylinder)
            node.simdPosition = (start + end) / 2
            node.simdOrientation = simd_quatf(
                from: SIMD3<Float>(0, 1, 0),
                to: delta / length
            )
            return node
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
