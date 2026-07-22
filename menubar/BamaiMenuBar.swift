// Bamai 菜单栏状态应用：轮询 /api/health 显示绿/黄/红圆点，提供打开控制台与启停入口。
// 服务进程本身由 launchd（com.bamai.app，KeepAlive）守护，本应用只是薄状态层，不监管 uvicorn。
// 通过 ./bamai menubar on 构建安装；项目路径在构建时写入 Info.plist 的 BamaiProjectDir。
import AppKit
import Foundation

enum ServiceState {
    case unreachable
    case ok
    case warn
    case critical

    var color: NSColor {
        switch self {
        case .unreachable: return .systemGray
        case .ok: return .systemGreen
        case .warn: return .systemYellow
        case .critical: return .systemRed
        }
    }
}

final class Localizer {
    private static let table: [String: (zh: String, en: String)] = [
        "open": ("打开控制台", "Open Dashboard"),
        "state.unreachable": ("服务未运行", "Service not running"),
        "state.ok": ("服务运行中 · 健康", "Service running · healthy"),
        "state.warn": ("服务运行中 · 注意", "Service running · warning"),
        "state.critical": ("服务运行中 · 异常", "Service running · critical"),
        "start": ("启动服务", "Start service"),
        "restart": ("重启服务", "Restart service"),
        "stop": ("停止服务", "Stop service"),
        "quit": ("退出菜单栏图标", "Quit menu bar icon"),
    ]

    static func language() -> String {
        let config = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".bamai/config.json")
        guard let data = try? Data(contentsOf: config),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let language = json["language"] as? String else { return "zh" }
        return language == "en" ? "en" : "zh"
    }

    static func text(_ key: String) -> String {
        guard let entry = table[key] else { return key }
        return language() == "en" ? entry.en : entry.zh
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let baseURL = URL(string: "http://127.0.0.1:8737")!
    private var statusItem: NSStatusItem!
    private var state: ServiceState = .unreachable
    private var timer: Timer?
    private let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 2
        configuration.timeoutIntervalForResource = 3
        return URLSession(configuration: configuration)
    }()

    private var projectDir: String {
        Bundle.main.object(forInfoDictionaryKey: "BamaiProjectDir") as? String ?? ""
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        applyState(.unreachable)
        pollHealth()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.pollHealth()
        }
    }

    private func pollHealth() {
        let request = URLRequest(url: baseURL.appendingPathComponent("api/health"))
        session.dataTask(with: request) { [weak self] data, response, _ in
            var next: ServiceState = .unreachable
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                switch json["level"] as? String {
                case "critical": next = .critical
                case "warn": next = .warn
                default: next = .ok
                }
            }
            DispatchQueue.main.async { self?.applyState(next) }
        }.resume()
    }

    private func applyState(_ next: ServiceState) {
        state = next
        statusItem.button?.image = dotImage(color: next.color)
    }

    private func dotImage(color: NSColor, diameter: CGFloat = 11) -> NSImage {
        let image = NSImage(size: NSSize(width: diameter, height: diameter), flipped: false) { rect in
            color.setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: 0.5, dy: 0.5)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        let stateKey: String
        switch state {
        case .unreachable: stateKey = "state.unreachable"
        case .ok: stateKey = "state.ok"
        case .warn: stateKey = "state.warn"
        case .critical: stateKey = "state.critical"
        }
        let stateItem = NSMenuItem(title: Localizer.text(stateKey), action: nil, keyEquivalent: "")
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())

        menu.addItem(makeItem("open", #selector(openDashboard)))
        if state == .unreachable {
            menu.addItem(makeItem("start", #selector(startService)))
        } else {
            menu.addItem(makeItem("restart", #selector(restartService)))
            menu.addItem(makeItem("stop", #selector(stopService)))
        }
        menu.addItem(.separator())
        menu.addItem(makeItem("quit", #selector(quit)))
    }

    private func makeItem(_ key: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: Localizer.text(key), action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    @objc private func openDashboard() {
        NSWorkspace.shared.open(baseURL)
    }

    @objc private func startService() { runBamai(["start"]) }
    @objc private func restartService() { runBamai(["restart"]) }
    @objc private func stopService() { runBamai(["stop"]) }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func runBamai(_ arguments: [String]) {
        let script = projectDir + "/bamai"
        guard FileManager.default.isExecutableFile(atPath: script) else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: script)
            process.arguments = arguments
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try? process.run()
            process.waitUntilExit()
            self?.pollHealth()
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
