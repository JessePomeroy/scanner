import Combine
import SceneKit
import SwiftUI
import UIKit

private struct OrientedBoxPointClassifier {
    let center: SIMD3<Double>
    let halfExtents: SIMD3<Double>
    let inverseQuaternion: SIMD4<Double>

    init?(_ region: ReconstructionRegion) {
        guard region.isValid else { return nil }
        center = SIMD3(region.center[0], region.center[1], region.center[2])
        halfExtents = SIMD3(region.extents[0], region.extents[1], region.extents[2]) / 2
        inverseQuaternion = SIMD4(
            -region.orientationXYZW[0],
            -region.orientationXYZW[1],
            -region.orientationXYZW[2],
            region.orientationXYZW[3]
        )
    }

    func contains(_ position: SIMD3<Float>) -> Bool {
        let value = SIMD3<Double>(position) - center
        let q = inverseQuaternion
        let tx = 2 * ((q.y * value.z) - (q.z * value.y))
        let ty = 2 * ((q.z * value.x) - (q.x * value.z))
        let tz = 2 * ((q.x * value.y) - (q.y * value.x))
        let local = SIMD3(
            value.x + (q.w * tx) + ((q.y * tz) - (q.z * ty)),
            value.y + (q.w * ty) + ((q.z * tx) - (q.x * tz)),
            value.z + (q.w * tz) + ((q.x * ty) - (q.y * tx))
        )
        let epsilon = 1e-9
        return abs(local.x) <= halfExtents.x + epsilon
            && abs(local.y) <= halfExtents.y + epsilon
            && abs(local.z) <= halfExtents.z + epsilon
    }
}

@MainActor
final class PointCloudPreviewStore: ObservableObject {
    @Published private(set) var preview: PointCloudPreview?
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?

    private let loader: PLYPointCloudLoader
    private var loadSequence = 0

    init(loader: PLYPointCloudLoader = PLYPointCloudLoader()) {
        self.loader = loader
    }

    func loadIfNeeded(fileURL: URL) async {
        guard preview == nil, !isLoading else { return }
        await load(fileURL: fileURL)
    }

    func load(fileURL: URL) async {
        loadSequence += 1
        let sequence = loadSequence
        preview = nil
        isLoading = true
        errorMessage = nil

        do {
            let loadedPreview = try await loader.load(fileURL: fileURL)
            guard !Task.isCancelled, sequence == loadSequence else { return }
            preview = loadedPreview
            isLoading = false
        } catch is CancellationError {
            cancel(sequence: sequence)
        } catch {
            guard sequence == loadSequence else { return }
            isLoading = false
            errorMessage = error.localizedDescription
        }
    }

    func deactivate() {
        loadSequence += 1
        isLoading = false
        errorMessage = nil
    }

    private func cancel(sequence: Int) {
        guard sequence == loadSequence else { return }
        isLoading = false
    }
}

struct PointCloudPreviewView: View {
    let download: DownloadedReconstructionArtifact
    let onDone: () -> Void

    @StateObject private var store = PointCloudPreviewStore()
    @State private var pointSize = 3.0
    @State private var reloadSequence = 0

    var body: some View {
        NavigationStack {
            content
                .navigationTitle(download.artifact.displayName)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done", action: onDone)
                    }
                }
        }
        .task(id: reloadSequence) {
            await store.loadIfNeeded(fileURL: download.fileURL)
        }
        .onDisappear {
            store.deactivate()
        }
    }

    @ViewBuilder
    private var content: some View {
        if let preview = store.preview {
            previewContent(preview)
        } else if store.isLoading {
            VStack(spacing: 16) {
                ProgressView()
                    .controlSize(.large)
                Text("Preparing point cloud")
                    .font(.headline)
                Text("Large files are sampled to keep the preview responsive.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .padding()
        } else {
            ContentUnavailableView {
                Label("Unable to Preview", systemImage: "exclamationmark.triangle")
            } description: {
                Text(store.errorMessage ?? "The point cloud could not be loaded.")
            } actions: {
                Button("Try Again") {
                    reloadSequence += 1
                }
            }
        }
    }

    private func previewContent(_ preview: PointCloudPreview) -> some View {
        PointCloudSceneView(
            preview: preview,
            pointSize: Float(pointSize),
            scopeRegion: nil,
            cameraPreview: nil
        )
        .ignoresSafeArea(edges: .bottom)
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 10) {
                HStack {
                    Label(pointSummary(preview), systemImage: "circle.grid.3x3.fill")
                    Spacer()
                    Text(preview.hasVertexColors ? "Vertex color" : "Preview color")
                }
                .font(.caption)
                .foregroundStyle(.secondary)

                HStack(spacing: 12) {
                    Image(systemName: "circle.fill")
                        .font(.system(size: 7))
                    Slider(value: $pointSize, in: 1...8, step: 0.5)
                        .accessibilityLabel("Point size")
                    Image(systemName: "circle.fill")
                        .font(.system(size: 15))
                }

                Text("Drag to orbit · Pinch to zoom · Two-finger drag to pan")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal)
            .padding(.vertical, 10)
            .background(.ultraThinMaterial)
        }
    }

    private func pointSummary(_ preview: PointCloudPreview) -> String {
        let sampled = Self.countFormatter.string(from: preview.sampledVertexCount as NSNumber)
            ?? String(preview.sampledVertexCount)
        guard preview.isDownsampled else { return "\(sampled) points" }
        let source = Self.countFormatter.string(from: preview.sourceVertexCount as NSNumber)
            ?? String(preview.sourceVertexCount)
        return "\(sampled) of \(source) points"
    }

    private static let countFormatter: NumberFormatter = {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        return formatter
    }()
}

struct PointCloudSceneView: UIViewRepresentable {
    let preview: PointCloudPreview
    let pointSize: Float
    let scopeRegion: ReconstructionRegion?
    let cameraPreview: SparseCameraPreview?

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeUIView(context: Context) -> FittedPointCloudSCNView {
        let view = FittedPointCloudSCNView(frame: .zero)
        view.backgroundColor = .black
        view.allowsCameraControl = true
        view.autoenablesDefaultLighting = false
        view.antialiasingMode = .multisampling4X
        view.preferredFramesPerSecond = 60
        configure(view, coordinator: context.coordinator)
        return view
    }

    func updateUIView(_ view: FittedPointCloudSCNView, context: Context) {
        if context.coordinator.previewID != preview.id {
            configure(view, coordinator: context.coordinator)
        } else if context.coordinator.renderedPointRegion != scopeRegion {
            updatePointGeometry(coordinator: context.coordinator)
        }
        context.coordinator.pointElement?.pointSize = CGFloat(pointSize)
        updateScopeNode(in: view, coordinator: context.coordinator)
        updateCameraNodes(in: view, coordinator: context.coordinator)
    }

    private func configure(_ view: FittedPointCloudSCNView, coordinator: Coordinator) {
        let scene = SCNScene()
        let rendered = makeGeometry()
        let pointsNode = SCNNode(geometry: rendered.geometry)
        scene.rootNode.addChildNode(pointsNode)

        let cameraNode = SCNNode()
        let camera = SCNCamera()
        camera.usesOrthographicProjection = true
        camera.zNear = 0.01
        camera.zFar = 100
        cameraNode.camera = camera
        cameraNode.position = SCNVector3(0, 0, 3.2)
        scene.rootNode.addChildNode(cameraNode)

        view.scene = scene
        view.pointOfView = cameraNode
        view.fittedCamera = camera
        view.contentHalfExtents = rendered.halfExtents
        view.setNeedsLayout()
        coordinator.previewID = preview.id
        coordinator.pointElement = rendered.element
        coordinator.sourceCenter = rendered.sourceCenter
        coordinator.sceneScale = rendered.sceneScale
        coordinator.scopeNode = nil
        coordinator.renderedRegion = nil
        coordinator.pointsNode = pointsNode
        coordinator.renderedPointRegion = scopeRegion
        coordinator.cameraNode = nil
        coordinator.renderedCameraPreview = nil
        rendered.element.pointSize = CGFloat(pointSize)
        updateScopeNode(in: view, coordinator: coordinator)
        updateCameraNodes(in: view, coordinator: coordinator)
    }

    private func updatePointGeometry(coordinator: Coordinator) {
        let rendered = makeGeometry()
        coordinator.pointsNode?.geometry = rendered.geometry
        coordinator.pointElement = rendered.element
        coordinator.renderedPointRegion = scopeRegion
        rendered.element.pointSize = CGFloat(pointSize)
    }

    private func updateScopeNode(
        in view: FittedPointCloudSCNView,
        coordinator: Coordinator
    ) {
        guard coordinator.renderedRegion != scopeRegion else { return }
        coordinator.scopeNode?.removeFromParentNode()
        coordinator.scopeNode = nil
        coordinator.renderedRegion = scopeRegion

        guard let region = scopeRegion,
              region.isValid,
              region.center.count == 3,
              region.extents.count == 3,
              region.orientationXYZW.count == 4,
              let scene = view.scene else { return }

        let scale = coordinator.sceneScale
        let box = SCNBox(
            width: CGFloat(region.extents[0]) * CGFloat(scale),
            height: CGFloat(region.extents[1]) * CGFloat(scale),
            length: CGFloat(region.extents[2]) * CGFloat(scale),
            chamferRadius: 0
        )
        let material = SCNMaterial()
        material.diffuse.contents = UIColor.systemCyan
        material.emission.contents = UIColor.systemCyan
        material.lightingModel = .constant
        material.fillMode = .lines
        material.isDoubleSided = true
        box.materials = [material]

        let node = SCNNode(geometry: box)
        node.position = SCNVector3(
            (Float(region.center[0]) - coordinator.sourceCenter.x) * scale,
            (Float(region.center[1]) - coordinator.sourceCenter.y) * scale,
            (Float(region.center[2]) - coordinator.sourceCenter.z) * scale
        )
        node.orientation = SCNQuaternion(
            Float(region.orientationXYZW[0]),
            Float(region.orientationXYZW[1]),
            Float(region.orientationXYZW[2]),
            Float(region.orientationXYZW[3])
        )
        scene.rootNode.addChildNode(node)
        coordinator.scopeNode = node
    }

    private func updateCameraNodes(
        in view: FittedPointCloudSCNView,
        coordinator: Coordinator
    ) {
        guard coordinator.renderedCameraPreview != cameraPreview else { return }
        coordinator.cameraNode?.removeFromParentNode()
        coordinator.cameraNode = nil
        coordinator.renderedCameraPreview = cameraPreview
        guard let cameraPreview,
              cameraPreview.isValid,
              let scene = view.scene else { return }

        let positions = cameraPreview.cameras.map { camera in
            SIMD3<Float>(
                (Float(camera.center[0]) - coordinator.sourceCenter.x) * coordinator.sceneScale,
                (Float(camera.center[1]) - coordinator.sourceCenter.y) * coordinator.sceneScale,
                (Float(camera.center[2]) - coordinator.sourceCenter.z) * coordinator.sceneScale
            )
        }
        let positionData = positions.withUnsafeBufferPointer(Data.init(buffer:))
        let source = SCNGeometrySource(
            data: positionData,
            semantic: .vertex,
            vectorCount: positions.count,
            usesFloatComponents: true,
            componentsPerVector: 3,
            bytesPerComponent: MemoryLayout<Float>.size,
            dataOffset: 0,
            dataStride: MemoryLayout<SIMD3<Float>>.stride
        )
        let pointIndices = (0..<positions.count).map(UInt32.init)
        let pointData = pointIndices.withUnsafeBufferPointer(Data.init(buffer:))
        let points = SCNGeometryElement(
            data: pointData,
            primitiveType: .point,
            primitiveCount: pointIndices.count,
            bytesPerIndex: MemoryLayout<UInt32>.size
        )
        points.pointSize = 7
        points.minimumPointScreenSpaceRadius = 4
        points.maximumPointScreenSpaceRadius = 10

        var elements = [points]
        if positions.count > 1 {
            var lineIndices: [UInt32] = []
            lineIndices.reserveCapacity((positions.count - 1) * 2)
            for index in 0..<(positions.count - 1) {
                lineIndices.append(UInt32(index))
                lineIndices.append(UInt32(index + 1))
            }
            let lineData = lineIndices.withUnsafeBufferPointer(Data.init(buffer:))
            elements.append(
                SCNGeometryElement(
                    data: lineData,
                    primitiveType: .line,
                    primitiveCount: positions.count - 1,
                    bytesPerIndex: MemoryLayout<UInt32>.size
                )
            )
        }

        let geometry = SCNGeometry(sources: [source], elements: elements)
        let material = SCNMaterial()
        material.diffuse.contents = UIColor.systemOrange
        material.emission.contents = UIColor.systemOrange
        material.lightingModel = .constant
        geometry.materials = Array(repeating: material, count: elements.count)
        let node = SCNNode(geometry: geometry)
        scene.rootNode.addChildNode(node)
        coordinator.cameraNode = node
    }

    private func makeGeometry() -> (
        geometry: SCNGeometry,
        element: SCNGeometryElement,
        halfExtents: SIMD3<Float>,
        sourceCenter: SIMD3<Float>,
        sceneScale: Float
    ) {
        let center = preview.bounds.center
        let extent = preview.bounds.largestExtent
        let scale: Float = extent > 0.000_001 ? 2 / extent : 1

        let positions = preview.vertices.map { vertex in
            (vertex.position - center) * scale
        }
        let classifier = scopeRegion.flatMap(OrientedBoxPointClassifier.init)
        let colors = preview.vertices.map { vertex in
            guard let classifier else { return vertex.color }
            return classifier.contains(vertex.position)
                ? vertex.color
                : SIMD4<Float>(0.22, 0.22, 0.25, 0.28)
        }
        let indices = (0..<preview.vertices.count).map(UInt32.init)

        let positionData = positions.withUnsafeBufferPointer(Data.init(buffer:))
        let colorData = colors.withUnsafeBufferPointer(Data.init(buffer:))
        let indexData = indices.withUnsafeBufferPointer(Data.init(buffer:))

        let positionSource = SCNGeometrySource(
            data: positionData,
            semantic: .vertex,
            vectorCount: positions.count,
            usesFloatComponents: true,
            componentsPerVector: 3,
            bytesPerComponent: MemoryLayout<Float>.size,
            dataOffset: 0,
            dataStride: MemoryLayout<SIMD3<Float>>.stride
        )
        let colorSource = SCNGeometrySource(
            data: colorData,
            semantic: .color,
            vectorCount: colors.count,
            usesFloatComponents: true,
            componentsPerVector: 4,
            bytesPerComponent: MemoryLayout<Float>.size,
            dataOffset: 0,
            dataStride: MemoryLayout<SIMD4<Float>>.stride
        )
        let element = SCNGeometryElement(
            data: indexData,
            primitiveType: .point,
            primitiveCount: indices.count,
            bytesPerIndex: MemoryLayout<UInt32>.size
        )
        element.minimumPointScreenSpaceRadius = 1
        element.maximumPointScreenSpaceRadius = 12

        let geometry = SCNGeometry(sources: [positionSource, colorSource], elements: [element])
        let material = SCNMaterial()
        material.lightingModel = .constant
        material.diffuse.contents = UIColor.white
        geometry.materials = [material]
        let sourceExtents = preview.bounds.maximum - preview.bounds.minimum
        return (geometry, element, (sourceExtents * scale) / 2, center, scale)
    }

    final class Coordinator {
        var previewID: UUID?
        weak var pointElement: SCNGeometryElement?
        weak var pointsNode: SCNNode?
        weak var scopeNode: SCNNode?
        weak var cameraNode: SCNNode?
        var renderedRegion: ReconstructionRegion?
        var renderedPointRegion: ReconstructionRegion?
        var renderedCameraPreview: SparseCameraPreview?
        var sourceCenter = SIMD3<Float>(repeating: 0)
        var sceneScale: Float = 1
    }
}

final class FittedPointCloudSCNView: SCNView {
    weak var fittedCamera: SCNCamera?
    var contentHalfExtents = SIMD3<Float>(repeating: 1)

    override func layoutSubviews() {
        super.layoutSubviews()
        guard bounds.width > 0, bounds.height > 0 else { return }
        let aspectRatio = Float(bounds.width / bounds.height)
        let boundingRadius = sqrt(
            (contentHalfExtents.x * contentHalfExtents.x)
                + (contentHalfExtents.y * contentHalfExtents.y)
                + (contentHalfExtents.z * contentHalfExtents.z)
        )
        let verticalHalfExtent = boundingRadius / min(max(aspectRatio, 0.01), 1)
        fittedCamera?.orthographicScale = Double(max(verticalHalfExtent * 1.15, 0.05))
    }
}
