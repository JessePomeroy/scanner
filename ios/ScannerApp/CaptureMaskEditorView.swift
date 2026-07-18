import SwiftUI

struct CaptureMaskEditorView: View {
    @Binding var polygon: [NormalizedMaskPoint]
    let onCancel: () -> Void
    let onConfirm: (CGSize) -> Void

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                Color.black.opacity(0.35)

                Color.clear
                    .contentShape(Rectangle())
                    .gesture(
                        DragGesture(minimumDistance: 0, coordinateSpace: .local)
                            .onChanged { value in
                                appendPoint(value.location, in: geometry.size)
                            }
                    )

                polygonPath(in: geometry.size)
                .fill(.white.opacity(polygon.count >= 3 ? 0.22 : 0))
                .allowsHitTesting(false)

                polygonPath(in: geometry.size)
                .stroke(.yellow, style: StrokeStyle(lineWidth: 3, lineJoin: .round))
                .allowsHitTesting(false)

                ForEach(Array(polygon.enumerated()), id: \.offset) { _, point in
                    Circle()
                        .fill(.yellow)
                        .frame(width: 10, height: 10)
                        .position(screenPoint(point, in: geometry.size))
                        .allowsHitTesting(false)
                }

                VStack(spacing: 12) {
                    HStack {
                        Button("Cancel", action: onCancel)
                        Spacer()
                        Button("Clear") {
                            polygon.removeAll()
                        }
                        .disabled(polygon.isEmpty)
                    }

                    Text("Trace around everything you want reconstructed")
                        .font(.headline)
                        .multilineTextAlignment(.center)

                    Spacer()

                    Text(isValidPolygon ? "Area selected" : "Drag to draw a closed area")
                        .font(.subheadline)

                    Button {
                        onConfirm(geometry.size)
                    } label: {
                        Label("Use This Area", systemImage: "checkmark.circle.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!isValidPolygon)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
                .foregroundStyle(.white)
            }
        }
        .background(.clear)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Reconstruction area editor")
    }

    private func appendPoint(_ point: CGPoint, in size: CGSize) {
        guard size.width > 0, size.height > 0 else { return }
        let normalized = NormalizedMaskPoint(
            x: Double(min(max(point.x / size.width, 0), 1)),
            y: Double(min(max(point.y / size.height, 0), 1))
        )
        if let previous = polygon.last {
            let dx = CGFloat(normalized.x - previous.x) * size.width
            let dy = CGFloat(normalized.y - previous.y) * size.height
            guard hypot(dx, dy) >= 4 else { return }
        }
        polygon.append(normalized)
    }

    private var isValidPolygon: Bool {
        do {
            try CaptureMaskRasterizer().validate(polygon)
            return true
        } catch {
            return false
        }
    }

    private func screenPoint(_ point: NormalizedMaskPoint, in size: CGSize) -> CGPoint {
        CGPoint(x: CGFloat(point.x) * size.width, y: CGFloat(point.y) * size.height)
    }

    private func polygonPath(in size: CGSize) -> Path {
        Path { path in
            guard let first = polygon.first else { return }
            path.move(to: screenPoint(first, in: size))
            for point in polygon.dropFirst() {
                path.addLine(to: screenPoint(point, in: size))
            }
            if polygon.count >= 3 {
                path.closeSubpath()
            }
        }
    }
}
