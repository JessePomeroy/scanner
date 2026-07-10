import SwiftUI

struct ProcessingHistoryView: View {
    @AppStorage("scanner.backendBaseURL") private var backendURLString = "http://localhost:8000"
    @StateObject private var store = ReconstructionJobStore(
        client: HTTPReconstructionJobClient()
    )

    var body: some View {
        NavigationStack {
            List {
                Section("Backend") {
                    TextField("http://192.168.1.10:8000", text: $backendURLString)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.go)
                        .onSubmit {
                            refresh()
                        }

                    Text("On iPhone, use the LAN address of the Mac or PC running the backend.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Button {
                        refresh()
                    } label: {
                        Label(
                            store.isLoading ? "Refreshing" : "Refresh Jobs",
                            systemImage: "arrow.clockwise"
                        )
                    }
                    .disabled(store.isLoading)
                }

                if let errorMessage = store.errorMessage {
                    Section {
                        Label {
                            Text(errorMessage)
                        } icon: {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                        }
                    }
                }

                Section("Recent Jobs") {
                    if store.isLoading && store.jobs.isEmpty {
                        HStack {
                            Spacer()
                            ProgressView("Loading jobs")
                            Spacer()
                        }
                    } else if store.jobs.isEmpty {
                        VStack(spacing: 8) {
                            Image(systemName: "clock.arrow.circlepath")
                                .font(.title2)
                                .foregroundStyle(.secondary)
                            Text(
                                store.hasLoaded
                                    ? "No processing jobs"
                                    : "Refresh to load processing history"
                            )
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 20)
                    } else {
                        ForEach(store.jobs) { job in
                            ReconstructionJobRow(job: job)
                        }
                    }
                }
            }
            .navigationTitle("Processing")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        refresh()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(store.isLoading)
                    .accessibilityLabel("Refresh processing jobs")
                }
            }
            .refreshable {
                await store.refresh(baseURLString: backendURLString)
            }
            .task {
                await store.loadIfNeeded(baseURLString: backendURLString)
            }
        }
    }

    private func refresh() {
        Task {
            await store.refresh(baseURLString: backendURLString)
        }
    }
}

private struct ReconstructionJobRow: View {
    let job: ReconstructionJob

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: job.status.systemImage)
                .foregroundStyle(statusColor)
                .font(.title3)
                .frame(width: 24)

            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(job.stage?.title ?? job.status.title)
                        .font(.subheadline.weight(.semibold))
                    Spacer(minLength: 8)
                    Text(job.status.title)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(statusColor)
                }

                Text(job.scanID)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)

                if let message = job.message, !message.isEmpty {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                HStack(spacing: 12) {
                    if let imageCount = job.imageCount {
                        Label("\(imageCount)", systemImage: "photo")
                    }
                    if let frameCount = job.frameCount {
                        Label("\(frameCount)", systemImage: "viewfinder")
                    }
                    if let date = job.updatedAt ?? job.createdAt {
                        Text(date, style: .relative)
                    }
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
    }

    private var statusColor: Color {
        switch job.status {
        case .received:
            return .blue
        case .processing:
            return .indigo
        case .validated, .complete:
            return .green
        case .failed:
            return .red
        case .unknown:
            return .secondary
        }
    }
}

@MainActor
final class ReconstructionJobStore: ObservableObject {
    @Published private(set) var jobs: [ReconstructionJob] = []
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var hasLoaded = false

    private let client: any ReconstructionJobLoading
    private var refreshSequence = 0
    private var historySource: String?

    init(client: any ReconstructionJobLoading) {
        self.client = client
    }

    func loadIfNeeded(baseURLString: String) async {
        guard !hasLoaded else { return }
        await refresh(baseURLString: baseURLString)
    }

    func refresh(baseURLString: String) async {
        refreshSequence += 1
        let sequence = refreshSequence
        isLoading = true
        errorMessage = nil

        let trimmedURL = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        if historySource != trimmedURL {
            jobs = []
            historySource = trimmedURL
        }
        guard let baseURL = URL(string: trimmedURL), !trimmedURL.isEmpty else {
            finish(sequence: sequence, error: ReconstructionJobClientError.invalidBaseURL)
            return
        }

        do {
            let loadedJobs = try await client.listJobs(baseURL: baseURL, limit: 50)
            guard !Task.isCancelled else {
                cancel(sequence: sequence)
                return
            }
            guard sequence == refreshSequence else { return }
            jobs = loadedJobs
            hasLoaded = true
            isLoading = false
        } catch is CancellationError {
            cancel(sequence: sequence)
        } catch let error as URLError where error.code == .cancelled {
            cancel(sequence: sequence)
        } catch {
            finish(sequence: sequence, error: error)
        }
    }

    private func cancel(sequence: Int) {
        guard sequence == refreshSequence else { return }
        isLoading = false
    }

    private func finish(sequence: Int, error: Error) {
        guard sequence == refreshSequence else { return }
        hasLoaded = true
        isLoading = false
        errorMessage = error.localizedDescription
    }
}

#Preview {
    ProcessingHistoryView()
}
