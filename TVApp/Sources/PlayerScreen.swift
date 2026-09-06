import AVKit
import SwiftUI

/// Native AVPlayerViewController. Its transport bar builds the Audio and
/// Subtitles menus from the file's own tracks, so there is nothing custom here.
struct PlayerScreen: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let controller = AVPlayerViewController()
        let player = AVPlayer(url: url)
        player.automaticallyWaitsToMinimizeStalling = true
        controller.player = player
        player.play()
        return controller
    }

    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {}

    static func dismantleUIViewController(_ controller: AVPlayerViewController, coordinator: ()) {
        controller.player?.pause()
        controller.player = nil
    }
}
