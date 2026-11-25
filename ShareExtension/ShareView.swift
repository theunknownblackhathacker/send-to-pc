import SwiftUI

struct ShareView: View {
    @ObservedObject var model: ShareModel

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if model.rows.isEmpty {
                        Text("Nothing to send.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(model.visibleRows) { row in
                        RowView(row: row)
                    }
                } footer: {
                    if model.allDone {
                        Label("Sent to your PC", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    }
                }

                Section {
                    TextField("http://100.82.199.69:8765", text: $model.server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit { model.saveSettings() }
                    SecureField("Token (optional)", text: $model.token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit { model.saveSettings() }
                } header: {
                    Text("PC server")
                } footer: {
                    Text("Change it here, then tap Retry.")
                }
            }
            .navigationTitle("Send to PC")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(model.allDone ? "Close" : "Cancel") {
                        model.allDone ? model.finish() : model.cancel()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if model.hasFailures && !model.running {
                        Button("Retry") {
                            Task { await model.retry() }
                        }
                    } else if model.running {
                        ProgressView()
                    }
                }
            }
        }
        .task { await model.start() }
    }
}

private struct RowView: View {
    let row: ShareModel.Row

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: row.icon)
                .font(.title3)
                .frame(width: 28)
                .foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 4) {
                Text(row.title)
                    .lineLimit(2)
                if !row.detail.isEmpty {
                    Text(row.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                switch row.state {
                case .sending:
                    ProgressView(value: row.progress)
                case .failed(let message):
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.red)
                default:
                    EmptyView()
                }
            }
            Spacer(minLength: 0)
            statusIcon
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private var statusIcon: some View {
        switch row.state {
        case .waiting:
            Image(systemName: "clock").foregroundStyle(.secondary)
        case .sending:
            EmptyView()
        case .done, .skipped:
            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
        }
    }
}
