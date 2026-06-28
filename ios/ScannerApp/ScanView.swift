import SwiftUI

struct ScanView: View {
    var body: some View {
        VStack(spacing: 16) {
            Text("Scanner")
                .font(.title)
            Text("AR capture view will be connected here.")
                .foregroundStyle(.secondary)
        }
        .padding()
    }
}

#Preview {
    ScanView()
}
