import Combine
import SceneKit
import SwiftUI
import UIKit

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
            pointSize: Float(pointSize)
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

private struct PointCloudSceneView: UIViewRepresentable {
    let preview: PointCloudPreview
    let pointSize: Float

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
        }
        context.coordinator.pointElement?.pointSize = CGFloat(pointSize)
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
        rendered.element.pointSize = CGFloat(pointSize)
    }

    private func makeGeometry() -> (
        geometry: SCNGeometry,
        element: SCNGeometryElement,
        halfExtents: SIMD3<Float>
    ) {
        let center = preview.bounds.center
        let extent = preview.bounds.largestExtent
        let scale: Float = extent > 0.000_001 ? 2 / extent : 1

        let positions = preview.vertices.map { vertex in
            (vertex.position - center) * scale
        }
        let colors = preview.vertices.map(\.color)
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
        return (geometry, element, (sourceExtents * scale) / 2)
    }

    final class Coordinator {
        var previewID: UUID?
        weak var pointElement: SCNGeometryElement?
    }
}

private final class FittedPointCloudSCNView: SCNView {
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
