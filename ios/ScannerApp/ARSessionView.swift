import ARKit
import SceneKit
import SwiftUI
import UIKit
import simd

struct ARSessionView: UIViewRepresentable {
    let session: ARSession
    var cameraPath: [SIMD3<Float>] = []
    var surfaceSamples: [SceneSurfaceSample] = []
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
        context.coordinator.updateSurfaceSamples(surfaceSamples, in: uiView)
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(onWorldTap: onWorldTap)
    }

    final class Coordinator: NSObject {
        var onWorldTap: ((SIMD3<Float>) -> Void)?
        private let pathRoot = SCNNode()
        private let surfaceRoot = SCNNode()
        private var renderedPath: [SIMD3<Float>] = []
        private var renderedSurfaceSamples: [SceneSurfaceSample] = []

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

        func updateSurfaceSamples(_ samples: [SceneSurfaceSample], in view: ARSCNView) {
            if surfaceRoot.parent == nil {
                surfaceRoot.name = "scene-surface-coverage"
                view.scene.rootNode.addChildNode(surfaceRoot)
            }
            guard samples != renderedSurfaceSamples else { return }
            surfaceRoot.childNodes.forEach { $0.removeFromParentNode() }
            samples.forEach { surfaceRoot.addChildNode(surfaceMarker(for: $0)) }
            renderedSurfaceSamples = samples
        }

        private func appendPath(_ newPoints: [SIMD3<Float>]) {
            for point in newPoints {
                if let previous = renderedPath.last,
                   simd_distance(previous, point) > 0.001,
                   simd_distance(previous, point) <= 1.25 {
                    pathRoot.addChildNode(pathSegment(from: previous, to: point))
                }
                pathRoot.addChildNode(pathMarker(at: point))
                renderedPath.append(point)
            }
        }

        private func surfaceMarker(for sample: SceneSurfaceSample) -> SCNNode {
            let sphere = SCNSphere(radius: sample.isWellCovered ? 0.035 : 0.025)
            sphere.segmentCount = 8
            let color: UIColor = sample.isWellCovered ? .systemGreen : .systemOrange
            sphere.firstMaterial?.diffuse.contents = color.withAlphaComponent(0.8)
            sphere.firstMaterial?.emission.contents = color.withAlphaComponent(0.25)
            let node = SCNNode(geometry: sphere)
            node.name = "scene-surface-\(sample.id)"
            node.simdPosition = sample.position
            return node
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
            let length = simd_length(delta)
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
