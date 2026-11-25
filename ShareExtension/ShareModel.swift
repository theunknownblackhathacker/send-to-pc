import Foundation
import UIKit

@MainActor
final class ShareModel: ObservableObject {
    enum State: Equatable {
        case waiting
        case sending
        case done
        case skipped
        case failed(String)
    }

    struct Row: Identifiable {
        let id = UUID()
        var title: String
        var icon: String
        var detail: String = ""
        var progress: Double = 0
        var state: State = .waiting
    }

    private enum Job {
        case provider(NSItemProvider)
        case text(String)
    }

    @Published var rows: [Row] = []
    @Published var running = false
    @Published var server: String = PCDropConfig.server
    @Published var token: String = PCDropConfig.token
    @Published var convertHEIC: Bool = PCDropConfig.convertHEIC {
        didSet { PCDropConfig.convertHEIC = convertHEIC }
    }

    private let context: NSExtensionContext?
    private var jobs: [Job] = []
    private var sentTexts = Set<String>()
    private let uploader = Uploader()
    private let tempDir: URL

    var hasFailures: Bool {
        rows.contains { if case .failed = $0.state { return true } else { return false } }
    }

    var allDone: Bool {
        !rows.isEmpty && rows.allSatisfy { $0.state == .done || $0.state == .skipped }
    }

    var visibleRows: [Row] {
        rows.filter { $0.state != .skipped }
    }

    init(context: NSExtensionContext?) {
        self.context = context
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("pcdrop-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)

        let items = (context?.inputItems ?? []).compactMap { $0 as? NSExtensionItem }
        var extraTexts: [String] = []
        for item in items {
            for provider in item.attachments ?? [] {
                jobs.append(.provider(provider))
                let label = Extractor.label(for: provider)
                rows.append(Row(title: label.title, icon: label.icon))
            }
            // Text that comes with the share (e.g. a post's caption next to its link).
            if let text = item.attributedContentText?.string
                .trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty {
                extraTexts.append(text)
            }
        }
        for text in extraTexts {
            jobs.append(.text(text))
            rows.append(Row(title: "Text", icon: "text.alignleft"))
        }
    }

    // MARK: - Sending

    func start() async {
        guard !running else { return }
        running = true
        for index in jobs.indices where rows[index].state != .done && rows[index].state != .skipped {
            await process(index)
        }
        running = false

        if rows.isEmpty {
            return
        }
        if allDone {
            try? await Task.sleep(nanoseconds: 700_000_000)
            finish()
        }
    }

    private func process(_ index: Int) async {
        rows[index].state = .sending
        rows[index].progress = 0
        rows[index].detail = ""
        do {
            let payload: Payload
            switch jobs[index] {
            case .text(let text):
                payload = .text(text)
            case .provider(let provider):
                payload = try await Extractor.extract(provider, into: tempDir)
            }

            switch payload {
            case .text(let text):
                let key = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if sentTexts.contains(key) {
                    rows[index].state = .skipped   // same link/text already sent
                    return
                }
                rows[index].title = shortened(key)
                if rows[index].icon == "shippingbox" { rows[index].icon = "text.alignleft" }
                try await uploader.sendText(text)
                sentTexts.insert(key)
            case .file(let original, _):
                let url = FileHelpers.jpegIfHEIC(original)
                let name = url.lastPathComponent
                rows[index].title = name
                let size = FileHelpers.size(of: url)
                rows[index].detail = ByteCountFormatter.string(fromByteCount: size, countStyle: .file)
                try await uploader.sendFile(url, name: name) { [weak self] fraction in
                    Task { @MainActor in
                        guard let self, self.rows.indices.contains(index) else { return }
                        self.rows[index].progress = fraction
                    }
                }
                try? FileManager.default.removeItem(at: url.deletingLastPathComponent())
            }
            rows[index].progress = 1
            rows[index].state = .done
        } catch {
            rows[index].state = .failed(error.localizedDescription)
        }
    }

    // MARK: - Actions

    func retry() async {
        saveSettings()
        for index in rows.indices {
            if case .failed = rows[index].state { rows[index].state = .waiting }
        }
        await start()
    }

    func saveSettings() {
        PCDropConfig.server = server
        PCDropConfig.token = token
    }

    func finish() {
        try? FileManager.default.removeItem(at: tempDir)
        context?.completeRequest(returningItems: nil)
    }

    func cancel() {
        try? FileManager.default.removeItem(at: tempDir)
        context?.cancelRequest(withError: NSError(domain: NSCocoaErrorDomain, code: NSUserCancelledError))
    }

    private func shortened(_ text: String) -> String {
        let oneLine = text.replacingOccurrences(of: "\n", with: " ")
        return oneLine.count > 80 ? String(oneLine.prefix(80)) + "…" : oneLine
    }
}
