import SwiftUI

@main
struct ATVCastApp: App {
    @StateObject private var server = ServerConfig()

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                SettingsView()
            }
            .environmentObject(server)
        }
    }
}

/// Server address (host:port) persisted in UserDefaults.
final class ServerConfig: ObservableObject {
    private static let key = "atvcast.server"

    @Published var address: String {
        didSet { UserDefaults.standard.set(address, forKey: Self.key) }
    }

    init() {
        address = UserDefaults.standard.string(forKey: Self.key) ?? "192.168.1.10:8080"
    }

    /// Normalises "192.168.1.10:8080" / "http://host:8080/" into a base URL.
    var baseURL: URL? {
        var text = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.lowercased().hasPrefix("http://") && !text.lowercased().hasPrefix("https://") {
            text = "http://" + text
        }
        while text.hasSuffix("/") { text.removeLast() }
        return URL(string: text)
    }

    func url(path: String) -> URL? {
        guard let base = baseURL else { return nil }
        return URL(string: path, relativeTo: base)?.absoluteURL
    }

    static var versionString: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "Version \(version) (build \(build))"
    }
}
