import SwiftUI

struct LibraryView: View {
    @EnvironmentObject private var server: ServerConfig
    @StateObject private var store = LibraryStore()
    @State private var playing: LibraryItem?

    var body: some View {
        Group {
            if store.loading && store.items.isEmpty {
                ProgressView("Loading library…")
            } else if store.items.isEmpty {
                VStack(spacing: 24) {
                    Text(store.error ?? "Nothing here yet.")
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Button("Retry") { Task { await store.load(from: server) } }
                }
                .padding(60)
            } else {
                List(store.items) { item in
                    Button {
                        playing = item
                    } label: {
                        HStack {
                            Text(item.title).lineLimit(1)
                            Spacer()
                            Text(item.durationText).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle("Library")
        .task { await store.load(from: server) }
        .fullScreenCover(item: $playing) { item in
            if let url = server.url(path: item.url) {
                PlayerScreen(url: url).ignoresSafeArea()
            } else {
                Text("Bad media URL").onAppear { playing = nil }
            }
        }
    }
}
