import SwiftUI

struct ScanView: View {
    @StateObject private var scanManager = ScanCaptureManager()
    @State private var shareURL: URL?

    var body: some View {
        ZStack(alignment: .bottom) {
            ARSessionView(session: scanManager.arSession)
                .ignoresSafeArea()

            VStack(spacing: 12) {
                statusBar
                controls
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 24)
        }
        .sheet(
            isPresented: Binding(
                get: { shareURL != nil },
                set: { isPresented in
                    if !isPresented {
                        shareURL = nil
                    }
                }
            )
        ) {
            if let shareURL {
                ShareSheet(items: [shareURL])
            }
        }
    }

    private var statusBar: some View {
        HStack(spacing: 12) {
            Label("\(scanManager.acceptedFrameCount)", systemImage: "photo.stack")
                .font(.headline.monospacedDigit())

            Text(scanManager.statusMessage)
                .font(.subheadline)
                .lineLimit(1)
                .minimumScaleFactor(0.75)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Button(action: primaryAction) {
                Label(primaryTitle, systemImage: primaryIcon)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(primaryActionDisabled)

            Button {
                shareURL = scanManager.lastZipURL
            } label: {
                Image(systemName: "square.and.arrow.up")
                    .frame(width: 44, height: 44)
            }
            .buttonStyle(.bordered)
            .disabled(scanManager.lastZipURL == nil)
            .accessibilityLabel("Share scan package")
        }
        .controlSize(.large)
    }

    private var primaryTitle: String {
        switch scanManager.state {
        case .idle, .completed, .failed:
            return "Start"
        case .scanning:
            return "Stop"
        case .exporting:
            return "Exporting"
        }
    }

    private var primaryIcon: String {
        switch scanManager.state {
        case .idle, .completed, .failed:
            return "record.circle"
        case .scanning:
            return "stop.fill"
        case .exporting:
            return "archivebox"
        }
    }

    private var primaryActionDisabled: Bool {
        if case .exporting = scanManager.state {
            return true
        }

        return false
    }

    private func primaryAction() {
        switch scanManager.state {
        case .scanning:
            stopScan()
        case .idle, .completed, .failed:
            startScan()
        case .exporting:
            break
        }
    }

    private func startScan() {
        do {
            try scanManager.startScan()
        } catch {
            scanManager.fail(error)
        }
    }

    private func stopScan() {
        do {
            shareURL = try scanManager.stopScan()
        } catch {
            scanManager.fail(error)
        }
    }
}

#Preview {
    ScanView()
}
