import Foundation
import UIKit
import UniformTypeIdentifiers

/// What one shared attachment turns into.
enum Payload {
    case text(String)
    case file(URL, name: String)
}

/// Turns any NSItemProvider (link, text, photo, video, document, folder, ...) into a Payload.
/// Not actor-isolated on purpose: NSItemProvider callbacks arrive on background queues.
enum Extractor {
    static func extract(_ provider: NSItemProvider, into dir: URL) async throws -> Payload {
        let types = provider.registeredTypeIdentifiers

        // 1. A real file (Files app, Mail attachments, ...). Folders arrive zipped.
        if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier),
           let url = urlValue(try? await loadItem(provider, UTType.fileURL.identifier)),
           url.isFileURL {
            let out = try FileHelpers.coordinatedCopy(url, into: dir)
            return .file(out, name: out.lastPathComponent)
        }

        // 2. A link (Safari, YouTube, X, ...).
        if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier),
           let url = urlValue(try? await loadItem(provider, UTType.url.identifier)) {
            if url.isFileURL {
                let out = try FileHelpers.coordinatedCopy(url, into: dir)
                return .file(out, name: out.lastPathComponent)
            }
            return .text(url.absoluteString)
        }

        // 3. Plain text (Notes, selected text, ...).
        if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier),
           let text = textValue(try? await loadItem(provider, UTType.plainText.identifier)),
           !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .text(text)
        }

        // 4. Anything else with bytes (photos, videos, PDFs, contacts, ...): stream it to a file.
        let dataTypes = types.filter { UTType($0)?.conforms(to: .data) ?? false }
        let candidates = unique(dataTypes + types)
        for type in candidates {
            if let out = try? await fileRepresentation(provider, type: type, into: dir) {
                return .file(out, name: out.lastPathComponent)
            }
        }
        for type in candidates {
            if let data = try? await dataRepresentation(provider, type: type) {
                let name = fileName(suggested: provider.suggestedName, fallback: "", type: type)
                let out = try FileHelpers.write(data, name: name, into: dir)
                return .file(out, name: out.lastPathComponent)
            }
        }

        // 5. Last resort: whatever object the app hands over.
        if let type = types.first, let item = try? await loadItem(provider, type) {
            if let image = item as? UIImage, let data = image.pngData() {
                let base = provider.suggestedName ?? "image"
                let out = try FileHelpers.write(data, name: base + ".png", into: dir)
                return .file(out, name: out.lastPathComponent)
            }
            if let data = item as? Data {
                let name = fileName(suggested: provider.suggestedName, fallback: "", type: type)
                let out = try FileHelpers.write(data, name: name, into: dir)
                return .file(out, name: out.lastPathComponent)
            }
            if let url = urlValue(item) {
                if url.isFileURL {
                    let out = try FileHelpers.coordinatedCopy(url, into: dir)
                    return .file(out, name: out.lastPathComponent)
                }
                return .text(url.absoluteString)
            }
            if let text = textValue(item) {
                return .text(text)
            }
        }

        throw UploadError.unsupported(types.joined(separator: ", "))
    }

    /// A short label for the row before the item is read.
    static func label(for provider: NSItemProvider) -> (title: String, icon: String) {
        let name = provider.suggestedName
        func has(_ t: UTType) -> Bool { provider.hasItemConformingToTypeIdentifier(t.identifier) }
        if has(.movie) { return (name ?? "Video", "film") }
        if has(.image) { return (name ?? "Photo", "photo") }
        if has(.pdf) { return (name ?? "PDF", "doc.richtext") }
        if has(.fileURL) { return (name ?? "File", "doc") }
        if has(.url) { return (name ?? "Link", "link") }
        if has(.plainText) { return (name ?? "Text", "text.alignleft") }
        return (name ?? "Item", "shippingbox")
    }

    // MARK: - NSItemProvider wrappers

    static func loadItem(_ provider: NSItemProvider, _ type: String) async throws -> NSSecureCoding? {
        try await withCheckedThrowingContinuation { continuation in
            provider.loadItem(forTypeIdentifier: type, options: nil) { item, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: item)
                }
            }
        }
    }

    /// The system's temp file is only valid inside the callback, so copy it there.
    static func fileRepresentation(_ provider: NSItemProvider, type: String, into dir: URL) async throws -> URL {
        let suggested = provider.suggestedName
        return try await withCheckedThrowingContinuation { continuation in
            _ = provider.loadFileRepresentation(forTypeIdentifier: type) { url, error in
                guard let url else {
                    continuation.resume(throwing: error ?? CocoaError(.fileReadUnknown))
                    return
                }
                do {
                    let name = fileName(suggested: suggested, fallback: url.lastPathComponent, type: type)
                    let out = try FileHelpers.uniqueURL(in: dir, name: name)
                    try FileManager.default.copyItem(at: url, to: out)
                    continuation.resume(returning: out)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    static func dataRepresentation(_ provider: NSItemProvider, type: String) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            _ = provider.loadDataRepresentation(forTypeIdentifier: type) { data, error in
                if let data {
                    continuation.resume(returning: data)
                } else {
                    continuation.resume(throwing: error ?? CocoaError(.fileReadUnknown))
                }
            }
        }
    }

    // MARK: - Value helpers

    static func urlValue(_ item: Any?) -> URL? {
        if let url = item as? URL { return url }
        if let url = item as? NSURL { return url as URL }
        if let data = item as? Data, let s = String(data: data, encoding: .utf8) {
            return URL(string: s.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        if let s = item as? String { return URL(string: s.trimmingCharacters(in: .whitespacesAndNewlines)) }
        return nil
    }

    static func textValue(_ item: Any?) -> String? {
        if let s = item as? String { return s }
        if let s = item as? NSAttributedString { return s.string }
        if let data = item as? Data { return String(data: data, encoding: .utf8) }
        if let url = item as? URL {
            if url.isFileURL { return try? String(contentsOf: url, encoding: .utf8) }
            return url.absoluteString
        }
        return nil
    }

    /// "IMG_1234" + public.heic -> "IMG_1234.heic"; keeps names that already have the extension.
    static func fileName(suggested: String?, fallback: String, type: String) -> String {
        let fallbackExt = (fallback as NSString).pathExtension
        let ext = fallbackExt.isEmpty ? (UTType(type)?.preferredFilenameExtension ?? "") : fallbackExt
        var base = (suggested ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if base.isEmpty { base = (fallback as NSString).deletingPathExtension }
        if base.isEmpty { base = "file" }
        if ext.isEmpty || (base as NSString).pathExtension.lowercased() == ext.lowercased() {
            return base
        }
        return base + "." + ext
    }

    private static func unique(_ list: [String]) -> [String] {
        var seen = Set<String>()
        return list.filter { seen.insert($0).inserted }
    }
}
