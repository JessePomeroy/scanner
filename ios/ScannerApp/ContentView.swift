import SwiftUI

struct ContentView: View {
    @State private var selectedTab = 0

    var body: some View {
        TabView(selection: $selectedTab) {
            ScanView()
                .tabItem {
                    Label("Scan", systemImage: "camera.viewfinder")
                }
                .tag(0)

            ScanGalleryView()
                .tabItem {
                    Label("Scans", systemImage: "archivebox")
                }
                .tag(1)

            ProcessingHistoryView {
                selectedTab = 0
            }
                .tabItem {
                    Label("Jobs", systemImage: "clock.arrow.circlepath")
                }
                .tag(2)
        }
    }
}

#Preview {
    ContentView()
}
