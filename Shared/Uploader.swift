import Foundation
import UniformTypeIdentifiers

/// Where the PC server is. Each target (app, share extension) keeps its own copy in its own
/// UserDefaults — no App Group, so signing with Feather stays simple.
enum PCDropConfig {
    static let defaultServer = "http://100.82.199.69:8765"
    private static let serverKey = "server"
    private static let tokenKey = "token"

    static var server: String {
        get { UserDefaults.standard.string(forKey: serverKey) ?? defaultServer }
        set { UserDefaults.standard.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: serverKey) }
    }

    static var token: String {
        get { UserDefaults.standard.string(forKey: tokenKey) ?? "" }
        set { UserDefaults.standard.set(newValue.trimmingCharacters(in: .whitespacesAndNewlines), forKey: tokenKey) }
    }

    static func endpoint(_ path: String) throws -> URL {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        let lower = s.lowercased()
        if !lower.hasPrefix("http://") && !lower.hasPrefix("https://") {
            s = "http://" + s
        }
        while s.hasSuffix("/") { s.removeLast() }
        guard let url = URL(string: s + path), url.host != nil else {
            throw UploadError.badServer(s)
        }
        return url
    }
}

enum UploadError: LocalizedError {
    case badServer(String)
    case http(Int, String)
    case unsupported(String)

    var errorDescription: String? {
        switch self {
        case .badServer(let s): return "Bad server address: \(s)"
        case .http(let code, let body): return "PC answered \(code): \(body)"
        case .unsupported(let types): return "Couldn't read this item (\(types))"
        }
    }
}

/// Sends text and files to server.py. Files are streamed from disk (low memory,
/// which matters in the share extension).
final class Uploader: NSObject, URLSessionTaskDelegate {
    private let lock = NSLock()
    private var progressHandler: ((Double) -> Void)?

    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 6 * 3600
        config.waitsForConnectivity = false
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    /// GET /items — returns how many items the PC already has.
    func ping() async throws -> Int {
        var request = URLRequest(url: try PCDropConfig.endpoint("/items"))
        request.timeoutInterval = 8
        let (data, response) = try await session.data(for: request)
        _ = try check(data, response)
        let list = (try? JSONSerialization.jsonObject(with: data)) as? [Any]
        return list?.count ?? 0
    }

    func sendText(_ text: String) async throws {
        var request = try makeRequest()
        request.setValue("text/plain; charset=utf-8", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.upload(for: request, from: Data(text.utf8))
        _ = try check(data, response)
    }

    func sendFile(_ fileURL: URL, name: String, progress: @escaping (Double) -> Void) async throws {
        var request = try makeRequest()
        request.setValue(FileHelpers.mimeType(for: name), forHTTPHeaderField: "Content-Type")
        request.setValue(FileHelpers.headerSafe(name), forHTTPHeaderField: "X-Filename")
        setProgress(progress)
        defer { setProgress(nil) }
        let (data, response) = try await session.upload(for: request, fromFile: fileURL, delegate: self)
        _ = try check(data, response)
        progress(1)
    }

    // MARK: - Helpers

    private func makeRequest() throws -> URLRequest {
        var request = URLRequest(url: try PCDropConfig.endpoint("/send"))
        request.httpMethod = "POST"
        let token = PCDropConfig.token
        if !token.isEmpty {
            request.setValue(token, forHTTPHeaderField: "X-Token")
        }
        return request
    }

    private func check(_ data: Data, _ response: URLResponse) throws -> String {
        let body = String(decoding: data, as: UTF8.self)
        guard let http = response as? HTTPURLResponse else { return body }
        guard (200..<300).contains(http.statusCode) else {
            throw UploadError.http(http.statusCode, body)
        }
        return body
    }

    private func setProgress(_ handler: ((Double) -> Void)?) {
        lock.lock()
        progressHandler = handler
        lock.unlock()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        lock.lock()
        let handler = progressHandler
        lock.unlock()
        handler?(Double(totalBytesSent) / Double(totalBytesExpectedToSend))
    }
}

enum FileHelpers {
    static func mimeType(for name: String) -> String {
        let ext = (name as NSString).pathExtension
        return UTType(filenameExtension: ext)?.preferredMIMEType ?? "application/octet-stream"
    }

    /// HTTP headers must be ASCII: percent-encode everything except [A-Za-z0-9-._].
    static func headerSafe(_ name: String) -> String {
        let allowed = CharacterSet(charactersIn:
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")
        return name.addingPercentEncoding(withAllowedCharacters: allowed) ?? "file"
    }

    /// A fresh subfolder per file so names never clash.
    static func uniqueURL(in dir: URL, name: String) throws -> URL {
        let folder = dir.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let clean = name.replacingOccurrences(of: "/", with: "_")
        return folder.appendingPathComponent(clean.isEmpty ? "file" : clean)
    }

    /// Copies a file (or a folder, zipped by the system) into `dir`.
    static func coordinatedCopy(_ source: URL, into dir: URL) throws -> URL {
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }

        var coordinationError: NSError?
        var result: Result<URL, Error> = .failure(CocoaError(.fileReadUnknown))
        // .forUploading gives the file itself, or a .zip when `source` is a folder.
        NSFileCoordinator().coordinate(readingItemAt: source, options: .forUploading,
                                       error: &coordinationError) { readURL in
            do {
                var name = readURL.lastPathComponent
                if (name as NSString).pathExtension.lowercased() == "zip",
                   (source.lastPathComponent as NSString).pathExtension.lowercased() != "zip" {
                    name = source.lastPathComponent + ".zip"
                }
                let out = try uniqueURL(in: dir, name: name)
                try FileManager.default.copyItem(at: readURL, to: out)
                result = .success(out)
            } catch {
                result = .failure(error)
            }
        }
        if let coordinationError { throw coordinationError }
        return try result.get()
    }

    static func write(_ data: Data, name: String, into dir: URL) throws -> URL {
        let out = try uniqueURL(in: dir, name: name)
        try data.write(to: out)
        return out
    }

    static func size(of url: URL) -> Int64 {
        let values = try? url.resourceValues(forKeys: [.fileSizeKey])
        return Int64(values?.fileSize ?? 0)
    }
}
