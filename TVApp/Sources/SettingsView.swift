import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var server: ServerConfig

    var body: some View {
        VStack(alignment: .leading, spacing: 40) {
            VStack(alignment: .leading, spacing: 8) {
                Text("atvcast").font(.largeTitle).bold()
                Text("Plays the MP4s remuxed by the atvcast server on your laptop.")
                    .foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 12) {
                Text("Server address").font(.headline)
                TextField("192.168.1.10:8080", text: $server.address)
                    .textContentType(.URL)
                    .autocorrectionDisabled()
                Text(server.baseURL.map { "Library: \($0.absoluteString)/library" }
                     ?? "Enter an IP address and port, e.g. 192.168.1.10:8080")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            NavigationLink("Open Library") {
                LibraryView()
            }
            .disabled(server.baseURL == nil)

            Spacer()

            Text(ServerConfig.versionString)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(60)
    }
}
