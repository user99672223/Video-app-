import Foundation

struct LibraryItem: Identifiable, Decodable, Hashable {
    let id: String
    let title: String
    let duration: Double?
    let size: Int64?
    let url: String

    var durationText: String {
        guard let d = duration, d > 0 else { return "" }
        let total = Int(d)
        let h = total / 3600, m = (total % 3600) / 60
        return h > 0 ? "\(h)h \(m)m" : "\(m)m"
    }
}

/// The server returns a bare JSON array; accept `{"items": [...]}` too so a
/// future server tweak does not break playback.
private struct LibraryEnvelope: Decodable {
    let items: [LibraryItem]
}

@MainActor
final class LibraryStore: ObservableObject {
    @Published var items: [LibraryItem] = []
    @Published var error: String?
    @Published var loading = false

    func load(from config: ServerConfig) async {
        guard let url = config.url(path: "/library") else {
            error = "Set a server address in Settings first."
            return
        }
        loading = true
        error = nil
        defer { loading = false }

        do {
            var request = URLRequest(url: url)
            request.cachePolicy = .reloadIgnoringLocalCacheData
            request.timeoutInterval = 15
            let (data, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                error = "Server returned HTTP \(http.statusCode)"
                return
            }
            let decoder = JSONDecoder()
            if let list = try? decoder.decode([LibraryItem].self, from: data) {
                items = list
            } else {
                items = try decoder.decode(LibraryEnvelope.self, from: data).items
            }
            if items.isEmpty { error = "Server has no converted items yet." }
        } catch {
            self.error = "Could not reach \(url.absoluteString): \(error.localizedDescription)"
        }
    }
}
