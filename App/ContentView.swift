import SwiftUI
import UIKit
import UniformTypeIdentifiers

struct ContentView: View {
    @State private var server = PCDropConfig.server
    @State private var token = PCDropConfig.token
    @State private var status = ""
    @State private var statusOK = true
    @State private var busy = false
    @State private var showImporter = false

    private let uploader = Uploader()

    var body: some View {
        NavigationStack {
            Form {
                Section("How to send") {
                    Label("In any app, tap Share", systemImage: "square.and.arrow.up")
                    Label("Pick PC Drop in the app row", systemImage: "desktopcomputer")
                    Label("Links, text, photos, videos, files, folders — anything", systemImage: "checkmark.circle")
                    Text("Not in the row? Scroll to the end → More → Edit, and add PC Drop to Favorites.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section {
                    TextField(PCDropConfig.defaultServer, text: $server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Token (optional)", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button {
                        Task { await test() }
                    } label: {
                        Label("Test connection", systemImage: "antenna.radiowaves.left.and.right")
                    }
                    .disabled(busy)
                } header: {
                    Text("PC server (this app)")
                } footer: {
                    Text("The share screen has its own server field at the bottom. Default for both: \(PCDropConfig.defaultServer)")
                }

                Section("Send from here") {
                    Button {
                        showImporter = true
                    } label: {
                        Label("Send files…", systemImage: "folder")
                    }
                    .disabled(busy)
                    Button {
                        Task { await sendClipboard() }
                    } label: {
                        Label("Send clipboard", systemImage: "doc.on.clipboard")
                    }
                    .disabled(busy)
                }

                if busy || !status.isEmpty {
                    Section {
                        HStack {
                            if busy { ProgressView().padding(.trailing, 6) }
                            Text(status)
                                .foregroundStyle(statusOK ? Color.primary : Color.red)
                        }
                    }
                }
            }
            .navigationTitle("PC Drop")
            .onChange(of: server) { _ in PCDropConfig.server = server }
            .onChange(of: token) { _ in PCDropConfig.token = token }
            .fileImporter(isPresented: $showImporter,
                          allowedContentTypes: [.item],
                          allowsMultipleSelection: true) { result in
                switch result {
                case .success(let urls):
                    Task { await sendFiles(urls) }
                case .failure(let error):
                    report(error.localizedDescription, ok: false)
                }
            }
        }
    }

    // MARK: - Actions

    private func test() async {
        busy = true
        status = "Connecting…"
        defer { busy = false }
        do {
            let count = try await uploader.ping()
            report("Connected ✓ — \(count) item(s) on the PC", ok: true)
        } catch {
            report(error.localizedDescription, ok: false)
        }
    }

    private func sendFiles(_ urls: [URL]) async {
        busy = true
        defer { busy = false }
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("pcdrop-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        var sent = 0
        for (i, url) in urls.enumerated() {
            do {
                status = "Preparing \(url.lastPathComponent)…"
                let copy = try FileHelpers.coordinatedCopy(url, into: dir)
                let name = copy.lastPathComponent
                try await uploader.sendFile(copy, name: name) { fraction in
                    Task { @MainActor in
                        status = "Sending \(i + 1)/\(urls.count): \(name) — \(Int(fraction * 100))%"
                    }
                }
                sent += 1
            } catch {
                report("\(url.lastPathComponent): \(error.localizedDescription)", ok: false)
                return
            }
        }
        report("Sent \(sent) file(s) ✓", ok: true)
    }

    private func sendClipboard() async {
        busy = true
        defer { busy = false }
        let board = UIPasteboard.general
        do {
            if let url = board.url {
                try await uploader.sendText(url.absoluteString)
                report("Sent link ✓", ok: true)
            } else if let text = board.string, !text.isEmpty {
                try await uploader.sendText(text)
                report("Sent text ✓", ok: true)
            } else if let image = board.image, let data = image.pngData() {
                let dir = FileManager.default.temporaryDirectory
                let file = try FileHelpers.write(data, name: "clipboard.png", into: dir)
                defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
                try await uploader.sendFile(file, name: "clipboard.png") { _ in }
                report("Sent image ✓", ok: true)
            } else {
                report("Clipboard is empty", ok: false)
            }
        } catch {
            report(error.localizedDescription, ok: false)
        }
    }

    private func report(_ message: String, ok: Bool) {
        status = message
        statusOK = ok
    }
}
