import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            ScanView()
                .tabItem {
                    Label("Scan", systemImage: "camera.viewfinder")
                }

            ScanGalleryView()
                .tabItem {
                    Label("Scans", systemImage: "archivebox")
                }

            ProcessingHistoryView()
                .tabItem {
                    Label("Jobs", systemImage: "clock.arrow.circlepath")
                }
        }
    }
}

#Preview {
    ContentView()
}
