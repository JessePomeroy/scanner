import SwiftUI

struct ScanView: View {
    @StateObject private var scanManager = ScanCaptureManager()
    @State private var shareURL: URL?

    var body: some View {
        ZStack(alignment: .bottom) {
            ARSessionView(session: scanManager.arSession) { worldPosition in
                scanManager.setObjectCenter(worldPosition)
            }
                .ignoresSafeArea()

            VStack(spacing: 12) {
                modeControls
                statusBar
                exportSummaryPanel
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
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Label("\(scanManager.acceptedFrameCount)", systemImage: "photo.stack")
                    .font(.headline.monospacedDigit())

                Label("\(scanManager.rejectedFrameCount)", systemImage: "xmark.circle")
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.secondary)

                if scanManager.scanMode == .object {
                    Image(systemName: "scope")
                        .foregroundStyle(scanManager.objectCenterIsSet ? .green : .secondary)
                }

                Text(scanManager.statusMessage)
                    .font(.subheadline)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)

                Spacer(minLength: 0)
            }

            HStack(spacing: 12) {
                metricLabel(
                    title: "Blur",
                    value: scanManager.lastBlurScore.map { String(format: "%.2f", $0) } ?? "--",
                    systemImage: "camera.metering.center.weighted"
                )

                metricLabel(
                    title: "Speed",
                    value: scanManager.lastMovementSpeed.map { String(format: "%.2fm/s", $0) } ?? "--",
                    systemImage: "speedometer"
                )

                Text(scanManager.guidanceMessage)
                    .font(.caption)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    .foregroundStyle(.secondary)

                Spacer(minLength: 0)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private func metricLabel(title: String, value: String, systemImage: String) -> some View {
        Label {
            Text("\(title) \(value)")
        } icon: {
            Image(systemName: systemImage)
        }
        .font(.caption.monospacedDigit())
        .foregroundStyle(.secondary)
    }

    @ViewBuilder
    private var exportSummaryPanel: some View {
        if let summary = scanManager.lastExportSummary {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    Image(systemName: "archivebox")
                        .foregroundStyle(.blue)

                    VStack(alignment: .leading, spacing: 2) {
                        Text(summary.scanId)
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(1)
                            .minimumScaleFactor(0.75)

                        Text(summary.zipFileName)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.75)
                    }

                    Spacer(minLength: 0)

                    Text(summary.scanModeTitle)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }

                LazyVGrid(
                    columns: [
                        GridItem(.flexible(), spacing: 8),
                        GridItem(.flexible(), spacing: 8),
                        GridItem(.flexible(), spacing: 8)
                    ],
                    spacing: 8
                ) {
                    summaryMetric(
                        title: "Frames",
                        value: "\(summary.acceptedFrameCount)",
                        systemImage: "photo.stack"
                    )
                    summaryMetric(
                        title: "Rejected",
                        value: "\(summary.rejectedFrameCount)",
                        systemImage: "xmark.circle"
                    )
                    summaryMetric(
                        title: "Blur",
                        value: summary.averageBlurScore.map { String(format: "%.2f", $0) } ?? "--",
                        systemImage: "camera.metering.center.weighted"
                    )
                    summaryMetric(
                        title: "Min Blur",
                        value: summary.minimumBlurScore.map { String(format: "%.2f", $0) } ?? "--",
                        systemImage: "camera.aperture"
                    )
                    summaryMetric(
                        title: "Speed",
                        value: summary.maximumMovementSpeedMetersPerSecond.map { String(format: "%.2fm/s", $0) } ?? "--",
                        systemImage: "speedometer"
                    )
                    summaryMetric(
                        title: "Time",
                        value: summary.captureDurationSeconds.map { String(format: "%.0fs", $0) } ?? "--",
                        systemImage: "timer"
                    )
                }

                if summary.objectRadiusMeters != nil || summary.objectCenterWasSet {
                    HStack(spacing: 12) {
                        Label(
                            summary.objectCenterWasSet ? "Subject set" : "No subject",
                            systemImage: summary.objectCenterWasSet ? "scope" : "questionmark.circle"
                        )

                        if let radius = summary.objectRadiusMeters {
                            Label(String(format: "%.2fm", radius), systemImage: "circle.dashed")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(.ultraThinMaterial)
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }

    private func summaryMetric(title: String, value: String, systemImage: String) -> some View {
        Label {
            VStack(alignment: .leading, spacing: 1) {
                Text(value)
                    .font(.caption.weight(.semibold).monospacedDigit())
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)

                Text(title)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
            }
        } icon: {
            Image(systemName: systemImage)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var modeControls: some View {
        VStack(spacing: 8) {
            Picker("Mode", selection: $scanManager.scanMode) {
                ForEach(ScanMode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .disabled(scanManager.state == .scanning || scanManager.state == .exporting)

            if scanManager.scanMode == .object {
                Picker("Radius", selection: $scanManager.objectRadiusPreset) {
                    ForEach(ObjectRadiusPreset.allCases) { preset in
                        Text(preset.title).tag(preset)
                    }
                }
                .pickerStyle(.segmented)
                .disabled(scanManager.state == .scanning || scanManager.state == .exporting)
            }
        }
        .padding(10)
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
            try scanManager.stopScan()
        } catch {
            scanManager.fail(error)
        }
    }
}

#Preview {
    ScanView()
}
