import AppKit
import Foundation
import SwiftUI

// MARK: - live.json (technical; never run ps)

enum Brand {
    static var chip: String {
        let env = ProcessInfo.processInfo.environment["LOCAL_AI_MONITOR_BRAND"] ?? "Monitor"
        return env.isEmpty ? "Runway" : env
    }
}

struct LiveSnap: Decodable {
    let ts: String
    let sample_interval_s: Double?
    let totals: Totals?
    let top: Top?
    let sessions: [Session]?
    let tools: ToolsMeta?
    /// Phase 2: needs_you / limited / ready_to_resume (empty = no strip)
    let attention: [AttentionItem]?
    let budgets: [String: BudgetItem]?
    let ccm: CcmMeta?

    struct Totals: Decodable {
        let cpu_pct: Double
        let rss_kb: Int
        let nproc: Int?
        let nsessions: Int?
    }

    struct Top: Decodable {
        let app: String?
        let session_id: String?
        let label: String?
        let cpu_pct: Double
        let rss_kb: Int
    }

    struct Session: Decodable {
        let app: String
        let session_id: String?
        let label: String?
        let detail: String?
        let cpu_pct: Double
        let rss_kb: Int
        let alive: Bool?
        let nproc: Int?
        let pids: [Int]?
        /// service | cli | desktop | agent | unknown
        let kind: String?
        let openable: Bool?
        let open_denied: String?
        let open_target: String?
        /// Plain-English: what this process is doing (justifies RAM).
        let activity: String?
        let title_hint: String?
        let subtitle_hint: String?
    }

    /// Catalog discovery + user visibility (from Python collector)
    struct ToolsMeta: Decodable {
        let installed: [String]?
        let hidden: [String]?
        let known: [KnownTool]?
        let visible: [String]?
        let running: [String]?
        let show_idle_installed: Bool?

        struct KnownTool: Decodable {
            let id: String
            let display: String
            let installed: Bool?
        }
    }

    struct AttentionItem: Decodable, Identifiable {
        let app: String
        let session_id: String?
        let kind: String  // needs_you | limited | ready_to_resume
        let title: String?
        let detail: String?
        let confidence: String?
        let resets_at: String?
        let cwd: String?

        var id: String {
            "\(app)|\(session_id ?? "")|\(kind)|\(title ?? "")"
        }
    }

    struct BudgetItem: Decodable {
        let week_tokens: Int?
        let week_label: String?
        let limit_state: String?
        let resets_at: String?
        let remaining_label: String?
    }

    /// Observer only — background learning; not efficiency claims
    struct CcmMeta: Decodable {
        let enabled: Bool?
        let last_run_ts: String?
        let last_pack: String?
        let note: String?
    }

    /// Physics-first free-RAM policy (local-ai-rm). show=false → hide strip.
    struct ResourceMeta: Decodable {
        let band: String?
        let show: Bool?
        let chip: String?
        let title: String?
        let detail: String?
        let candidate_label: String?
        let candidate_app: String?
        let candidate_session_id: String?
        let action_label: String?
        let free_mb: Int?
        let headroom_mb: Int?
        let headroom_ok_mb: Int?
        let headroom_warn_mb: Int?
        let memsize_mb: Int?
        let ai_mem_pct: Double?
        let cpu_count: Int?
        let cpu_capacity_pct: Double?
        let heavy_rss_kb: Int?
        let learned_ai_rss_mb: Double?
        let learned_ai_mem_pct: Double?
        let learned_cpu_capacity_pct: Double?
        let learned_thrash_score: Double?
        let profile_status: String?
        let profile_age_s: Double?
        let profile_confidence: Double?
        let host_id: String?
        let swap_used_mb: Int?
        let swap_total_mb: Int?
        let thrash_score: Double?
        let pressure_state: String?
        let recommendation: String?
        let can_start_heavy: Bool?
        let plain_state: String?
        let plain_title: String?
        let plain_detail: String?
        let next_step: String?
        let promise: String?
        let runway_action: String?
        let checkpoint_hint: String?
        let browser_hint: String?
        let swapins: UInt64?
        let swapouts: UInt64?
        let pageouts: UInt64?
        let ai_rss_mb: Int?
        let urgency: String?
        let managed: Bool?
        let managed_label: String?
        let auto_end: Bool?
    }

    /// Per-tool 12h CPU-seconds series (30m buckets) for sparklines.
    let sparks: [String: [Double]]?
    let resource: ResourceMeta?
    let quarantine: QuarantineMeta?

    struct QuarantineMeta: Decodable {
        let tools: [String]?
    }
}

/// User prefs — same schema as Python ~/.config/local-ai-monitor/tools.json
struct ToolPrefsFile: Codable {
    var hidden: [String]
    var order: [String]
    var show_idle_installed: Bool
    var custom_ids: [String]

    static let empty = ToolPrefsFile(
        hidden: [], order: [], show_idle_installed: true, custom_ids: []
    )

    init(
        hidden: [String],
        order: [String],
        show_idle_installed: Bool,
        custom_ids: [String]
    ) {
        self.hidden = hidden
        self.order = order
        self.show_idle_installed = show_idle_installed
        self.custom_ids = custom_ids
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hidden = try c.decodeIfPresent([String].self, forKey: .hidden) ?? []
        order = try c.decodeIfPresent([String].self, forKey: .order) ?? []
        show_idle_installed = try c.decodeIfPresent(Bool.self, forKey: .show_idle_installed) ?? true
        custom_ids = try c.decodeIfPresent([String].self, forKey: .custom_ids) ?? []
    }
}

struct EditToolRow: Identifiable {
    let id: String
    let title: String
    let installed: Bool
    var isShown: Bool
}

// Activity state for non-tech labels
enum ActivityState: String, Equatable {
    case active = "Active"   // working hard
    case open = "Open"       // running, quiet
    case idle = "Idle"       // barely any CPU

    static func from(cpu: Double, alive: Bool?) -> ActivityState {
        if alive == false { return .idle }
        if cpu >= 5 { return .active }
        if cpu >= 0.3 { return .open }
        return .idle
    }

    var tint: Color {
        switch self {
        case .active: return .orange
        case .open: return .green
        case .idle: return .secondary
        }
    }

    var symbol: String {
        switch self {
        case .active: return "bolt.circle.fill"
        case .open: return "circle.fill"
        case .idle: return "moon.zzz.fill"
        }
    }
}

/// Glance load band for By-tool rows (ops glass: red / amber / green / muted).
enum LoadBand: String {
    case hot      // very busy CPU or heavy RAM
    case busy     // working / busy
    case quiet    // light / open but running
    case idle     // installed · quiet

    /// Floor only. Runtime labels use the host-derived resource.heavy_rss_kb.
    static let heavyRssFloorKb = 1024 * 1024

    static func from(cpu: Double, rssKb: Int, sessionCount: Int, heavyRssKb: Int = heavyRssFloorKb) -> LoadBand {
        if sessionCount <= 0 && cpu <= 0 && rssKb <= 0 { return .idle }
        // Hot = very high CPU or host-scale heavy RAM.
        if cpu >= 50 || rssKb >= heavyRssKb { return .hot }
        if cpu >= 5 || rssKb >= (512 * 1024) { return .busy }  // ≥512 MB = busy tint
        return .quiet
    }

    var tint: Color {
        switch self {
        case .hot: return .red
        case .busy: return .orange
        case .quiet: return .green
        case .idle: return .secondary
        }
    }
}

// MARK: - Human copy (mirrors local_ai_monitor/humanize.py)

enum Human {
    static let toolNames: [String: String] = [
        "OpenClaw": "OpenClaw",
        "Grok": "Grok",
        "Claude CLI": "Anthropic CLI",
        "Claude Desktop": "Co-Work",
        "Buzz": "Buzz",
        "ChatGPT": "ChatGPT",
        "Codex": "Codex",
        "OpenAI CLI": "OpenAI CLI",
        "Cursor": "Cursor",
    ]

    static let channelTitles: [String: String] = [
        "sample-app": "Sample App",
        "sample-meshsim": "Sample MeshSim",
        "desktop": "Buzz desktop",
        "unknown": "Unknown project",
    ]

    static let agentTitles: [String: String] = [
        "operator/grok": "Operator (Grok)",
        "operator": "Operator",
        "tunnel": "Network tunnel",
        "channel-trace": "Channel monitor",
        "runtime": "App runtime",
        "worker": "Worker",
        "managed": "Buzz helper",
        "acp-worker": "Agent worker",
    ]

    static func toolName(_ app: String) -> String {
        toolNames[app] ?? app
    }

    static func titleWords(_ slug: String) -> String {
        slug.replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .split(separator: " ")
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: " ")
    }

    static func channelName(_ ch: String) -> String {
        let c = ch.lowercased()
        return channelTitles[c] ?? titleWords(ch)
    }

    static func agentName(_ ag: String) -> String {
        let a = ag.lowercased()
        if let t = agentTitles[a] { return t }
        if a.contains("/") {
            let parts = a.split(separator: "/", maxSplits: 1).map(String.init)
            if parts.count == 2 {
                let l = agentTitles[parts[0]] ?? titleWords(parts[0])
                let r = agentTitles[parts[1]] ?? titleWords(parts[1])
                return "\(l) (\(r))"
            }
        }
        return titleWords(ag)
    }

    static func loadPhrase(_ cpu: Double) -> String {
        if cpu < 0.5 { return "Quiet" }
        if cpu < 5 { return "Light" }
        if cpu < 20 { return "Working" }
        if cpu < 50 { return "Busy" }
        return "Very busy"
    }

    static func memShort(_ rssKb: Int) -> String {
        let mb = Double(rssKb) / 1024.0
        if rssKb <= 0 { return "—" }
        if mb < 1 { return "\(rssKb) KB" }
        if mb < 10 { return String(format: "%.1f MB", mb) }
        if mb < 1024 { return "\(Int(mb.rounded())) MB" }
        return String(format: "%.1f GB", mb / 1024.0)
    }

    static func memPhrase(_ rssKb: Int) -> String {
        let mb = Double(rssKb) / 1024.0
        if rssKb <= 0 { return "no memory listed" }
        if mb < 1024 { return "about \(Int(max(1, mb.rounded()))) MB of memory" }
        return String(format: "about %.1f GB of memory", mb / 1024.0)
    }

    static func mbShort(_ mb: Int) -> String {
        if mb < 0 { return "—" }
        if mb >= 1000 { return String(format: "%.1f GB", Double(mb) / 1024.0) }
        return "\(mb) MB"
    }

    static func resourceSummary(headroomMb: Int?, swapUsedMb: Int?, swapTotalMb: Int?, thrash: Double?, aiMb: Int?) -> String {
        var parts: [String] = []
        if let headroomMb {
            parts.append("Room \(mbShort(headroomMb))")
        }
        if let swapUsedMb {
            if let swapTotalMb, swapTotalMb > 0 {
                parts.append("swap \(mbShort(swapUsedMb)) / \(mbShort(swapTotalMb))")
            } else {
                parts.append("swap \(mbShort(swapUsedMb))")
            }
        }
        if let thrash {
            parts.append(String(format: "swap activity %.1f", thrash))
        }
        if let aiMb, aiMb > 0 {
            parts.append("AI work \(mbShort(aiMb))")
        }
        return parts.joined(separator: " · ")
    }

    static func pressureLabel(_ state: String, band: String) -> String {
        switch state {
        case "calibrating": return "Calibrating"
        case "ok": return "Open"
        case "caution": return "Watch"
        case "stop_start_gate": return "Hold"
        case "freeze_risk": return "Protect"
        case "unknown": return "—"
        default: return band == "hard" ? "Protect" : (band == "warn" ? "Hold" : "Open")
        }
    }

    static func pressureTitle(_ state: String, band: String, canStart: Bool) -> String {
        switch state {
        case "calibrating":
            return "Calibrating this Mac"
        case "ok":
            return "You can keep working"
        case "caution":
            return "Keep an eye on this Mac"
        case "stop_start_gate":
            return "Do not start more heavy work"
        case "freeze_risk":
            return "Protect the work that is open"
        case "unknown":
            return "Runway cannot read this Mac yet"
        default:
            if band == "hard" { return "Protect the work that is open" }
            if !canStart { return "Do not start more heavy work" }
            return "You can keep working"
        }
    }

    static func pressureDetail(_ state: String, band: String, canStart: Bool) -> String {
        switch state {
        case "calibrating":
            return "Collecting a first local baseline before treating capacity numbers as final."
        case "ok":
            return "This Mac has room. Starts are safe."
        case "caution":
            return "Starts are still allowed — do not pile on several heavy apps at once."
        case "stop_start_gate":
            return "Pause unused background work before opening more. Mid-stream work stays open."
        case "freeze_risk":
            return "This Mac is shuffling memory. Your open work stays safe."
        case "unknown":
            return "Avoid starting another heavy app until Runway can read this Mac."
        default:
            if band == "hard" {
                return "This Mac is shuffling memory. Your open work stays safe."
            }
            if !canStart {
                return "Pause unused background work before opening more. Mid-stream work stays open."
            }
            return "This Mac has room. Starts are safe."
        }
    }

    static func checkpointAction(app: String) -> String? {
        switch app {
        case "Claude CLI":
            return "Run /compact, then resume with claude --continue."
        case "Codex":
            return "Run /compact, then resume with codex resume."
        case "Grok":
            return "Copy the task state before closing this session."
        case "Cursor":
            return "Save files and wait for agent tasks to finish."
        case "ChatGPT", "Claude Desktop":
            return "Let the response finish; keep the chat open."
        default:
            return nil
        }
    }

    static func chip(cpu: Double, stale: Bool) -> String {
        if stale { return "\(Brand.chip) · —" }
        if cpu < 0.5 { return "\(Brand.chip) · Quiet" }
        if cpu < 15 { return "\(Brand.chip) · \(Int(cpu.rounded()))%" }
        return "\(Brand.chip) · Busy \(Int(cpu.rounded()))%"
    }

    static func sentence(cpu: Double, rssKb: Int, topApp: String?, topCpu: Double, stale: Bool) -> String {
        if stale { return "The background monitor is not updating." }
        let memGb = Double(rssKb) / (1024.0 * 1024.0)
        if cpu >= 80 || memGb >= 4 {
            if let t = topApp { return "AI tools are loading this Mac heavily — mainly \(toolName(t))." }
            return "AI tools are loading this Mac heavily."
        }
        if topCpu >= 40 || cpu >= 15 || memGb >= 2 {
            if let t = topApp, topCpu >= 5 { return "\(toolName(t)) is working hard." }
            return "Some AI tools are using noticeable resources."
        }
        return "All AI tools look fine."
    }

    static func parseBuzz(label: String?, sessionId: String?) -> (String, String) {
        if let sid = sessionId, sid.hasPrefix("buzz:"), sid.contains("|") {
            let rest = String(sid.dropFirst(5))
            let parts = rest.split(separator: "|", maxSplits: 1).map(String.init)
            if parts.count == 2 { return (parts[0], parts[1]) }
        }
        if let lab = label, lab.contains(" · ") {
            let parts = lab.components(separatedBy: " · ")
            if parts.count >= 2 {
                return (parts[0], parts.dropFirst().joined(separator: " · "))
            }
        }
        return (label ?? "unknown", "worker")
    }
}

// MARK: - Rows

struct ToolRow: Identifiable {
    let id: String          // internal app id e.g. "Grok"
    let title: String
    /// Load + session count only — never includes mem (mem is a fixed trailing column).
    let detail: String
    let mem: String
    let sessionCount: Int
    let cpu: Double
    let rssKb: Int
    let isQuietInstalled: Bool
    let loadBand: LoadBand
    let spark: [Double]
    /// Owns the single heaviest live session (for hot wash).
    var ownsHeaviest: Bool = false
}

struct SessionRow: Identifiable {
    let id: String
    let app: String
    let title: String
    let subtitle: String
    let state: ActivityState
    let detailPath: String?
    let sessionId: String?
    let label: String?
    let cpu: Double
    let mem: String
    let rssKb: Int
    /// Process count (multi-pid trees need confirm before end).
    let nproc: Int
    /// Deterministic open: false → grey arrow, no fake “Opened”.
    let openable: Bool
    let openDenied: String
    let kind: String
    /// What it is doing (RAM justification).
    let activity: String
    var isHeavy: Bool { rssKb >= LoadBand.heavyRssFloorKb }
    var isMultiPid: Bool { nproc > 1 }
    /// Conservative fallback for non-model call sites.
    var needsEndConfirm: Bool { isHeavy || isMultiPid }
    var isService: Bool { kind == "service" }
    var checkpointAction: String? { Human.checkpointAction(app: app) }
    var needsCheckpoint: Bool {
        checkpointAction != nil && !isService && (state == .active || state == .open)
    }
}

struct BuzzWorkerRow: Identifiable {
    let id: String
    let title: String
    let session: SessionRow
}

struct BuzzProjectRow: Identifiable {
    let id: String
    let title: String
    let workers: [BuzzWorkerRow]
}

struct ParkingItem: Identifiable, Decodable {
    let id: String
    let title: String?
    let subtitle: String?
    let app: String?
    let rss_mb: Int?
    let action_label: String?
    let reason: String?
}

struct ParkingSummary: Decodable {
    let parkable_count: Int?
    let parkable_mb: Int?
    let protected_count: Int?
}

struct ParkingSnap: Decodable {
    let ok: Bool?
    let message: String?
    let detail: String?
    let parkable: [ParkingItem]?
    let protected: [ParkingItem]?
    let already_parked: [ParkingItem]?
    let parked: [ParkingItem]?
    let summary: ParkingSummary?
    let acted: Bool?
    let dry_run: Bool?
}

enum ParkingCopy {
    static func summary(items: [ParkingItem]) -> String {
        if items.isEmpty {
            return "Only active work remains."
        }
        if items.count == 1, let first = items.first {
            let title = first.title ?? first.app ?? "Background helper"
            return "Ready to park: \(title) · \(Human.mbShort(first.rss_mb ?? 0))"
        }
        let mb = items.map { $0.rss_mb ?? 0 }.reduce(0, +)
        return "Ready to park: \(items.count) helpers · \(Human.mbShort(mb))"
    }
}

struct BrowserHelperItem: Identifiable, Decodable {
    var id: String { "\(pid ?? 0)-\(name ?? "")-\(kind ?? "")" }
    let pid: Int?
    let rss_mb: Int?
    let kind: String?
    let name: String?
}

struct BrowserItem: Identifiable, Decodable {
    let id: String
    let app: String?
    let category: String?
    let rss_mb: Int?
    let processes: Int?
    let renderer_processes: Int?
    let windows: Int?
    let tabs: Int?
    let frontmost: Bool?
    let closeable: Bool?
    let recommendation: String?
    let top_helpers: [BrowserHelperItem]?
}

struct BrowserSummary: Decodable {
    let candidate_count: Int?
    let candidate_mb: Int?
    let protected_count: Int?
}

struct BrowserSnap: Decodable {
    let ok: Bool?
    let frontmost: String?
    let message: String?
    let detail: String?
    let candidates: [BrowserItem]?
    let protected: [BrowserItem]?
    let summary: BrowserSummary?
}

enum BrowserCopy {
    static func summary(items: [BrowserItem]) -> String {
        if items.isEmpty { return "No inactive app load found." }
        if items.count == 1, let first = items.first {
            return "Review: \(first.app ?? "Browser") · \(Human.mbShort(first.rss_mb ?? 0))"
        }
        let mb = items.map { $0.rss_mb ?? 0 }.reduce(0, +)
        return "Review: \(items.count) apps · \(Human.mbShort(mb))"
    }
}

enum MonitorCopy {
    static var physicalMemoryMb: Double {
        Double(ProcessInfo.processInfo.physicalMemory) / (1024.0 * 1024.0)
    }

    static func roomFullMb() -> Double {
        return min(4096.0, max(1500.0, physicalMemoryMb * 0.125))
    }

    static func shufflingLabel(_ score: Double?) -> String {
        guard let score else { return "Unknown" }
        if score >= 25 { return "Severe" }
        if score >= 8 { return "High" }
        if score >= 2 { return "Active" }
        return "Low"
    }

    static func shufflingTint(_ score: Double?) -> Color {
        guard let score else { return .secondary }
        if score >= 25 { return .red }
        if score >= 8 { return .orange }
        if score >= 2 { return .yellow }
        return .green
    }

    static func roomRatio(_ mb: Int?, okMb: Int? = nil) -> Double {
        guard let mb else { return 0 }
        let full = (okMb ?? 0) > 0 ? Double(okMb ?? 0) : roomFullMb()
        return min(1.0, max(0.0, Double(mb) / full))
    }

    static func roomTint(_ mb: Int?, okMb: Int? = nil, warnMb: Int? = nil) -> Color {
        guard let mb else { return .secondary }
        if let warnMb, warnMb > 0, mb < warnMb { return .red }
        let r = roomRatio(mb, okMb: okMb)
        if r < 0.40 { return .red }
        if r < 0.75 { return .orange }
        return .green
    }

    static func backupRatio(used: Int?, total: Int?) -> Double {
        guard let used, let total, total > 0 else { return 0 }
        return min(1.0, max(0.0, Double(used) / Double(total)))
    }

    static func backupTint(used: Int?, total: Int?) -> Color {
        let r = backupRatio(used: used, total: total)
        if r >= 0.80 { return .red }
        if r >= 0.50 { return .orange }
        if r > 0 { return .yellow }
        return .green
    }

    static func aiRedlineMb() -> Double {
        // AI tools are guests on the machine. If they alone consume about a
        // quarter of physical memory, they are crowding out the OS and apps.
        max(512.0, physicalMemoryMb * 0.25)
    }

    static func aiRatio(_ mb: Int?) -> Double {
        guard let mb else { return 0 }
        return min(1.0, max(0.0, Double(mb) / aiRedlineMb()))
    }

    static func aiTint(_ mb: Int?) -> Color {
        let r = aiRatio(mb)
        if r >= 1.0 { return .red }
        if r >= 0.75 { return .orange }
        if r >= 0.50 { return .yellow }
        return .green
    }

    static func percentLabel(_ pct: Double?) -> String {
        guard let pct else { return "Unknown" }
        return String(format: "%.0f%%", pct)
    }

    static func aiMemTint(_ pct: Double?) -> Color {
        guard let pct else { return .secondary }
        if pct >= 35 { return .red }
        if pct >= 25 { return .orange }
        if pct >= 15 { return .yellow }
        return .green
    }

    static func cpuTint(_ pct: Double?) -> Color {
        guard let pct else { return .secondary }
        if pct >= 85 { return .red }
        if pct >= 65 { return .orange }
        if pct >= 40 { return .yellow }
        return .green
    }

    static func actionTitle(state: String, canStart: Bool) -> String {
        switch state {
        case "freeze_risk":
            return "Leave open work alone"
        case "stop_start_gate":
            return "Do not start more heavy work"
        case "caution":
            return "You can still start more"
        default:
            return canStart ? "Nothing to do" : "Do not start more heavy work"
        }
    }

    static func actionSubtitle(state: String, canStart: Bool) -> String {
        switch state {
        case "freeze_risk":
            return "Keep chats open. Pause unused background work only if the Mac feels stuck."
        case "stop_start_gate":
            return "Pause unused background work first. Your chats stay open."
        case "caution":
            return "This Mac is under watch, but starts are still allowed."
        default:
            return canStart ? "Runway will warn you before starts become risky." : "Pause unused background work first. Your chats stay open."
        }
    }
}

enum NavLevel: Equatable {
    case home
    case tool(String) // app id
    case editTools
    case newSession
    case parkingLot
    case browserCleanup
    case more
}

struct AttentionRow: Identifiable {
    let id: String
    let app: String
    let kind: String
    let title: String
    let detail: String
    let confidence: String
    let resetsAt: String?
    let cwd: String?
    /// Claude/Grok conversation id when known (UUID). Used for real resume, not bare relaunch.
    let sessionId: String?
    let canResume: Bool

    var isNeedsYou: Bool { kind == "needs_you" }
    var isLimited: Bool { kind == "limited" }
    var isReady: Bool { kind == "ready_to_resume" }

    var tint: Color {
        if isReady { return .green }
        if isNeedsYou { return .orange }
        if isLimited { return .red }
        return .secondary
    }

    var symbol: String {
        if isReady { return "arrow.clockwise.circle.fill" }
        if isNeedsYou { return "hand.raised.fill" }
        if isLimited { return "hourglass.circle.fill" }
        return "exclamationmark.circle"
    }
}

// MARK: - Launch new session (Terminal / desktop app)

enum LaunchHelper {
    private static var prefsURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/local-ai-monitor/launch.json")
    }

    static func lastCwd(for toolId: String) -> String {
        if let data = try? Data(contentsOf: prefsURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let map = obj["last_cwd"] as? [String: String],
           let cwd = map[toolId], !cwd.isEmpty,
           FileManager.default.fileExists(atPath: cwd) {
            return cwd
        }
        let gp = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Projects").path
        if FileManager.default.fileExists(atPath: gp) { return gp }
        return FileManager.default.homeDirectoryForCurrentUser.path
    }

    static func rememberCwd(_ cwd: String, toolId: String) {
        var map: [String: String] = [:]
        if let data = try? Data(contentsOf: prefsURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let prev = obj["last_cwd"] as? [String: String] {
            map = prev
        }
        map[toolId] = cwd
        let dir = prefsURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let payload: [String: Any] = ["last_cwd": map]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted]) {
            try? data.write(to: prefsURL, options: .atomic)
        }
    }

    /// Launch tool; returns short status string for focusNote.
    /// - Parameters:
    ///   - resume: When true, continue prior conversation (Claude: `--resume` / `-c`).
    ///   - sessionId: Conversation UUID when known (not `pid:…` live keys).
    @discardableResult
    static func launch(
        toolId: String,
        cwd: String?,
        sessionId: String? = nil,
        resume: Bool = false
    ) -> String {
        let dir = (cwd?.isEmpty == false ? cwd! : lastCwd(for: toolId))
        rememberCwd(dir, toolId: toolId)
        switch toolId {
        case "Claude Desktop":
            activateApp(bundleId: "com.anthropic.claudefordesktop", path: "/Applications/Claude.app")
            return "Opened Claude app"
        case "ChatGPT":
            activateApp(bundleId: "com.openai.chat", path: "/Applications/ChatGPT.app")
            return "Opened ChatGPT"
        case "Buzz":
            activateApp(bundleId: "xyz.block.buzz.app", path: "/Applications/Buzz.app")
            return "Opened Buzz"
        case "Cursor":
            // Open workspace folder in Cursor when known
            if FileManager.default.fileExists(atPath: dir) {
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
                proc.arguments = ["-a", "/Applications/Cursor.app", dir]
                try? proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus == 0 {
                    return "Opened Cursor · \((dir as NSString).lastPathComponent)"
                }
                NSWorkspace.shared.open(URL(fileURLWithPath: dir))
            }
            activateApp(bundleId: "com.todesktop.230313mzl4w4u92", path: "/Applications/Cursor.app")
            return "Opened Cursor"
        case "Grok":
            let bin = resolveBin("grok") ?? "grok"
            // Grok has no stable --resume UUID surface here; open CLI in project dir.
            return runInTerminal(command: shellQuote(bin), cwd: dir, label: "Grok")
        case "Claude CLI":
            let bin = resolveBin("claude") ?? "claude"
            let cmd: String
            let label: String
            if resume, let sid = conversationUUID(sessionId) {
                cmd = "\(shellQuote(bin)) --resume \(shellQuote(sid))"
                label = "Anthropic CLI (resume)"
            } else if resume {
                // Most recent conversation in this directory
                cmd = "\(shellQuote(bin)) --continue"
                label = "Anthropic CLI (continue)"
            } else {
                cmd = shellQuote(bin)
                label = "Anthropic CLI"
            }
            return runInTerminal(command: cmd, cwd: dir, label: label)
        case "Codex":
            let bin = resolveBin("codex") ?? "codex"
            if resume {
                if let sid = conversationUUID(sessionId) {
                    return runInTerminal(
                        command: "\(shellQuote(bin)) resume \(shellQuote(sid))",
                        cwd: dir,
                        label: "Codex (resume)"
                    )
                }
                return runInTerminal(
                    command: "\(shellQuote(bin)) resume --last",
                    cwd: dir,
                    label: "Codex (resume last)"
                )
            }
            return runInTerminal(command: shellQuote(bin), cwd: dir, label: "Codex")
        case "OpenAI CLI":
            let bin = resolveBin("openai") ?? "openai"
            return runInTerminal(command: shellQuote(bin), cwd: dir, label: "OpenAI CLI")
        case "OpenClaw":
            let bin = resolveBin("openclaw") ?? "openclaw"
            return runInTerminal(command: shellQuote(bin), cwd: dir, label: "OpenClaw")
        default:
            return runInTerminal(command: shellQuote(toolId.lowercased()), cwd: dir, label: toolId)
        }
    }

    /// UUID-shaped conversation ids only (drop live `pid:…` keys).
    private static func conversationUUID(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        if raw.hasPrefix("pid:") { return nil }
        // 8-4-4-4-12 hex UUID
        let parts = raw.split(separator: "-")
        guard parts.count == 5,
              parts[0].count == 8,
              parts[1].count == 4,
              parts[2].count == 4,
              parts[3].count == 4,
              parts[4].count == 12,
              raw.allSatisfy({ $0.isHexDigit || $0 == "-" })
        else { return nil }
        return raw
    }

    private static func resolveBin(_ name: String) -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "\(home)/.local/bin/\(name)",
            "/opt/homebrew/bin/\(name)",
            "/usr/local/bin/\(name)",
            "/usr/bin/\(name)",
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private static func shellQuote(_ s: String) -> String {
        if s.isEmpty { return "''" }
        if s.rangeOfCharacter(from: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "/._-")).inverted) == nil {
            return s
        }
        return "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// Open Terminal with a real shell script.
    /// Prefers `open -a Terminal foo.command` (no Automation TCC) over AppleScript.
    private static func runInTerminal(command: String, cwd: String, label: String) -> String {
        let base = (cwd as NSString).lastPathComponent
        logAction("launch label=\(label) cwd=\(cwd) cmd=\(command)")

        if let scriptURL = writeLaunchCommand(command: command, cwd: cwd) {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            proc.arguments = ["-a", "Terminal", scriptURL.path]
            do {
                try proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus == 0 {
                    return "Started \(label) in \(base)"
                }
                logAction("open -a Terminal failed status=\(proc.terminationStatus)")
            } catch {
                logAction("open -a Terminal error=\(error)")
            }
        }

        // Fallback: AppleScript (needs Automation permission for Terminal)
        let escDir = cwd.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let escCmd = command.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = """
        tell application "Terminal"
          activate
          do script "cd \\"\(escDir)\\" && \(escCmd)"
        end tell
        """
        var error: NSDictionary?
        if NSAppleScript(source: script)?.executeAndReturnError(&error) != nil {
            return "Started \(label) in \(base)"
        }
        if let error {
            logAction("osascript error=\(error)")
        }
        activateApp(bundleId: "com.apple.Terminal", path: "/System/Applications/Utilities/Terminal.app")
        return "Could not run automatically — open Terminal and run: \(command)"
    }

    private static func writeLaunchCommand(command: String, cwd: String) -> URL? {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("local-ai-monitor-launch", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("run-\(UUID().uuidString.prefix(8)).command")
        let body = """
        #!/bin/zsh
        export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
        cd \(shellQuote(cwd)) || {
          echo "local-ai-monitor: could not cd to \(cwd)" >&2
          read -k1 -s '?Press any key…'
          exit 1
        }
        exec \(command)
        """
        do {
            try body.write(to: url, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: url.path
            )
            return url
        } catch {
            logAction("writeLaunchCommand error=\(error)")
            return nil
        }
    }

    private static func logAction(_ msg: String) {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let path = home.appendingPathComponent(".local/state/local-ai-monitor/menubar-actions.log")
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(msg)\n"
        if let data = line.data(using: .utf8) {
            if FileManager.default.fileExists(atPath: path.path),
               let handle = try? FileHandle(forWritingTo: path) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            } else {
                try? data.write(to: path, options: .atomic)
            }
        }
    }

    private static func activateApp(bundleId: String, path: String) {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = true
            NSWorkspace.shared.openApplication(at: url, configuration: cfg)
            return
        }
        let url = URL(fileURLWithPath: path)
        if FileManager.default.fileExists(atPath: path) {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = true
            NSWorkspace.shared.openApplication(at: url, configuration: cfg)
        }
    }
}

// MARK: - End confirm (multi-pid or ≥400 MB)

enum EndConfirm {
    /// Blocking AppKit alert — reliable inside MenuBarExtra (SwiftUI sheets flake).
    @discardableResult
    static func ask(title: String, message: String, actionTitle: String = "End") -> Bool {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: actionTitle)
        alert.addButton(withTitle: "Cancel")
        // Menubar is accessory — activate so the sheet is frontmost
        NSApp.activate(ignoringOtherApps: true)
        return alert.runModal() == .alertFirstButtonReturn
    }
}

// MARK: - End session (kill RAM / stuck tools)

enum QuarantineHelper {
    static func add(_ tool: String) -> String {
        EndSessionHelper.runPublic(["quarantine", "add", tool])
    }

    static func remove(_ tool: String) -> String {
        EndSessionHelper.runPublic(["quarantine", "remove", tool])
    }
}

enum MonitorCLI {
    static func executable() -> (executable: String, prefixArgs: [String]) {
        let env = ProcessInfo.processInfo.environment
        if let explicit = env["LOCAL_AI_MONITOR_CLI"], !explicit.isEmpty,
           FileManager.default.isExecutableFile(atPath: explicit) {
            return (explicit, [])
        }
        let home = LivePathConfig.home
        let candidates = [
            home.appendingPathComponent(".local/bin/local-ai-monitor").path,
            "/opt/homebrew/bin/local-ai-monitor",
            "/usr/local/bin/local-ai-monitor",
        ]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            return (c, [])
        }
        return ("/usr/bin/env", ["local-ai-monitor"])
    }
}

enum ParkingLotHelper {
    static func status() -> ParkingSnap? {
        run(["parking-lot", "status", "--state", LivePathConfig.stateDir.path, "--json"])
    }

    static func apply() -> ParkingSnap? {
        run(["parking-lot", "apply", "--apply", "--state", LivePathConfig.stateDir.path, "--json"])
    }

    private static func run(_ args: [String]) -> ParkingSnap? {
        let proc = Process()
        let cli = MonitorCLI.executable()
        proc.executableURL = URL(fileURLWithPath: cli.executable)
        proc.arguments = cli.prefixArgs + args
        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        var env = ProcessInfo.processInfo.environment
        env["LOCAL_AI_MONITOR_STATE"] = LivePathConfig.stateDir.path
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        proc.environment = env
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            return ParkingSnap(
                ok: false,
                message: "Could not read Parking Lot.",
                detail: error.localizedDescription,
                parkable: [],
                protected: [],
                already_parked: [],
                parked: [],
                summary: nil,
                acted: false,
                dry_run: nil
            )
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        if proc.terminationStatus == 0,
           let snap = try? JSONDecoder().decode(ParkingSnap.self, from: data) {
            return snap
        }
        let e = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return ParkingSnap(
            ok: false,
            message: e.isEmpty ? "Parking Lot failed." : e,
            detail: "",
            parkable: [],
            protected: [],
            already_parked: [],
            parked: [],
            summary: nil,
            acted: false,
            dry_run: nil
        )
    }
}

enum BrowserCleanupHelper {
    static func status() -> BrowserSnap? {
        run(["browser-advice", "--json", "--min-rss-mb", "20"])
    }

    static func close(app: String) -> BrowserSnap? {
        _ = run(["browser-advice", "--json", "--min-rss-mb", "20", "--quit-app", app])
        return status()
    }

    static func closeAll() -> BrowserSnap? {
        _ = run(["browser-advice", "--json", "--min-rss-mb", "20", "--quit-all"])
        return status()
    }

    private static func run(_ args: [String]) -> BrowserSnap? {
        let proc = Process()
        let cli = MonitorCLI.executable()
        proc.executableURL = URL(fileURLWithPath: cli.executable)
        proc.arguments = cli.prefixArgs + args
        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        proc.environment = env
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            return BrowserSnap(
                ok: false,
                frontmost: nil,
                message: "Could not read browser load.",
                detail: error.localizedDescription,
                candidates: [],
                protected: [],
                summary: nil
            )
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        if proc.terminationStatus == 0,
           let snap = try? JSONDecoder().decode(BrowserSnap.self, from: data) {
            return snap
        }
        let e = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return BrowserSnap(
            ok: false,
            frontmost: nil,
            message: e.isEmpty ? "Browser cleanup unavailable." : e,
            detail: "",
            candidates: [],
            protected: [],
            summary: nil
        )
    }
}

enum EndSessionHelper {
    /// Resolve `local-ai-monitor` CLI (install path first).
    private static var aiTopBin: String {
        let candidates = [
            FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".local/bin/local-ai-monitor").path,
            "/opt/homebrew/bin/local-ai-monitor",
            "/usr/local/bin/local-ai-monitor",
        ]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            return c
        }
        return "local-ai-monitor"
    }

    static func end(tool: String, sessionId: String) -> String {
        run(["end-session", "--tool", tool, "--session", sessionId])
    }

    static func endAll(tool: String) -> String {
        run(["end-session", "--tool", tool, "--all"])
    }

    static func endHeaviest() -> String {
        run(["end-session", "--heaviest"])
    }

    /// Shared by quarantine helper.
    static func runPublic(_ args: [String]) -> String { run(args) }

    private static func run(_ args: [String]) -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: aiTopBin)
        // If path is bare name, use env -i free PATH shell
        if aiTopBin == "local-ai-monitor" {
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["local-ai-monitor"] + args
        } else {
            proc.arguments = args
        }
        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        // Prefer source package if present (dev), else installed lib
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let lib = "\(home)/.local/lib/local-ai-monitor"
        env["PYTHONPATH"] = lib
        proc.environment = env
        do {
            try proc.run()
            proc.waitUntilExit()
        } catch {
            return "Could not run local-ai-monitor end-session: \(error.localizedDescription)"
        }
        let o = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let e = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if proc.terminationStatus == 0 {
            return o.isEmpty ? "Ended." : o
        }
        return e.isEmpty ? (o.isEmpty ? "End failed." : o) : e
    }
}

// MARK: - Focus / open (deterministic — never claim success without a real target)

struct OpenResult {
    let ok: Bool
    let message: String
}

enum FocusHelper {
    /// Open a session only when there is a known target. Never “check Terminal or the app.”
    static func open(_ row: SessionRow) -> OpenResult {
        if !row.openable {
            let reason = row.openDenied.isEmpty
                ? "Cannot open — no window or app for this session"
                : row.openDenied
            return OpenResult(ok: false, message: reason)
        }

        switch row.app {
        case "Claude Desktop":
            if activateApp(bundleId: "com.anthropic.claudefordesktop", path: "/Applications/Claude.app") {
                return OpenResult(ok: true, message: "Opened Co-Work")
            }
            return OpenResult(ok: false, message: "Could not open Co-Work")
        case "ChatGPT":
            if activateApp(bundleId: "com.openai.chat", path: "/Applications/ChatGPT.app") {
                return OpenResult(ok: true, message: "Opened ChatGPT")
            }
            return OpenResult(ok: false, message: "Could not open ChatGPT")
        case "Buzz":
            if let path = buzzProjectPath(from: row), FileManager.default.fileExists(atPath: path) {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
                return OpenResult(ok: true, message: "Opened Buzz project folder")
            }
            if activateApp(bundleId: "xyz.block.buzz.app", path: "/Applications/Buzz.app") {
                return OpenResult(ok: true, message: "Opened Buzz")
            }
            return OpenResult(ok: false, message: "Could not open Buzz")
        case "Grok", "Claude CLI", "Codex", "OpenAI CLI":
            if focusTerminal(matching: row) {
                return OpenResult(ok: true, message: "Focused Terminal window")
            }
            if let path = row.detailPath, path.hasPrefix("/"),
               FileManager.default.fileExists(atPath: path) {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
                return OpenResult(ok: true, message: "Opened project folder")
            }
            return OpenResult(
                ok: false,
                message: "No Terminal window or folder found for this session"
            )
        case "OpenClaw":
            // Service — never pretend Terminal/app open
            return OpenResult(
                ok: false,
                message: "Background gateway — no app window. Use Quarantine to stop it."
            )
        case "Cursor":
            if activateApp(bundleId: "com.todesktop.230313mzl4w4u92", path: "/Applications/Cursor.app") {
                return OpenResult(ok: true, message: "Opened Cursor")
            }
            return OpenResult(ok: false, message: "Could not open Cursor")
        default:
            if focusTerminal(matching: row) {
                return OpenResult(ok: true, message: "Focused Terminal window")
            }
            if let path = row.detailPath, path.hasPrefix("/"),
               FileManager.default.fileExists(atPath: path) {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
                return OpenResult(ok: true, message: "Opened folder")
            }
            return OpenResult(ok: false, message: "No known window or app for this session")
        }
    }

    @discardableResult
    private static func activateApp(bundleId: String, path: String) -> Bool {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = true
            NSWorkspace.shared.openApplication(at: url, configuration: cfg)
            return true
        }
        let url = URL(fileURLWithPath: path)
        if FileManager.default.fileExists(atPath: path) {
            let cfg = NSWorkspace.OpenConfiguration()
            cfg.activates = true
            NSWorkspace.shared.openApplication(at: url, configuration: cfg)
            return true
        }
        return false
    }

    /// Match Terminal window titles (often include folder basename or app name).
    @discardableResult
    private static func focusTerminal(matching row: SessionRow) -> Bool {
        var needles: [String] = []
        if let path = row.detailPath, path.hasPrefix("/") {
            needles.append((path as NSString).lastPathComponent)
        }
        if let lab = row.label, !lab.isEmpty, !lab.hasPrefix("pid") {
            let left = lab.components(separatedBy: " · ").first ?? lab
            if !left.hasPrefix("pid") { needles.append(left) }
        }
        if row.app == "Grok" { needles.append("grok") }
        if row.app == "Claude CLI" { needles.append("claude") }
        if row.app == "Codex" { needles.append("codex") }
        if row.app == "OpenAI CLI" { needles.append("openai") }
        needles = needles.filter { !$0.isEmpty }
        guard !needles.isEmpty else { return false }

        // AppleScript: find window whose name contains any needle
        let quoted = needles.map { $0.replacingOccurrences(of: "\"", with: "\\\"") }
        let conditions = quoted.map { "name of w contains \"\($0)\"" }.joined(separator: " or ")
        let script = """
        tell application "Terminal"
          activate
          set matched to false
          repeat with w in windows
            try
              if \(conditions) then
                set index of w to 1
                set matched to true
                exit repeat
              end if
            end try
          end repeat
          return matched
        end tell
        """
        var error: NSDictionary?
        if let obj = NSAppleScript(source: script)?.executeAndReturnError(&error) {
            return obj.booleanValue
        }
        return false
    }

    private static func buzzProjectPath(from row: SessionRow) -> String? {
        // detail may be "channel=… path=buzz-foo" or a real path
        if let d = row.detailPath, d.hasPrefix("/") { return d }
        let (ch, _) = Human.parseBuzz(label: row.label, sessionId: row.sessionId)
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "\(home)/.buzz/PROJECTS/buzz-\(ch)",
            "\(home)/.buzz/PROJECTS/\(ch)",
            "\(home)/.buzz/REPOS/\(ch)",
            "\(home)/.buzz/REPOS/buzz-\(ch)",
        ]
        return candidates.first { FileManager.default.fileExists(atPath: $0) }
    }
}

// MARK: - Live path resolution (A/B: production vs native lab)

/// Resolve which live snapshot file the menu bar reads.
///
/// Priority:
/// 1. `LOCAL_AI_MONITOR_LIVE` — absolute path to live.json / live.min.json
/// 2. `LOCAL_AI_MONITOR_STATE` — `$LOCAL_AI_MONITOR_STATE/live.min.json` if present, else `live.json`
/// 3. Default production: `~/.local/state/local-ai-monitor/live.json`
///
/// Lab A/B never rewrites production paths; only changes which file is read.
enum LivePathConfig {
    static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    static var defaultProductionLive: URL {
        home.appendingPathComponent(".local/state/local-ai-monitor/live.json")
    }

    static var defaultProductionState: URL {
        home.appendingPathComponent(".local/state/local-ai-monitor")
    }

    /// True only for a *non-production state tree* (e.g. local-ai-monitor-native lab).
    /// Reading `live.min.json` under `~/.local/state/local-ai-monitor` is **default**, not lab.
    static var isLab: Bool {
        stateDir.standardizedFileURL != defaultProductionState.standardizedFileURL
    }

    static var stateDir: URL {
        let env = ProcessInfo.processInfo.environment
        if let s = env["LOCAL_AI_MONITOR_STATE"], !s.isEmpty {
            return URL(fileURLWithPath: (s as NSString).expandingTildeInPath, isDirectory: true)
        }
        // Infer from LOCAL_AI_MONITOR_LIVE parent only when that parent is not production.
        if let raw = env["LOCAL_AI_MONITOR_LIVE"], !raw.isEmpty {
            let live = URL(fileURLWithPath: (raw as NSString).expandingTildeInPath)
            let parent = live.deletingLastPathComponent().standardizedFileURL
            if parent != defaultProductionState.standardizedFileURL {
                return parent
            }
        }
        return defaultProductionState
    }

    static func newestLiveURL(in dir: URL) -> URL {
        let min = dir.appendingPathComponent("live.min.json")
        let full = dir.appendingPathComponent("live.json")
        let fm = FileManager.default
        guard fm.isReadableFile(atPath: min.path) else { return full }
        guard fm.isReadableFile(atPath: full.path) else { return min }

        let minDate = (try? fm.attributesOfItem(atPath: min.path)[.modificationDate] as? Date) ?? .distantPast
        let fullDate = (try? fm.attributesOfItem(atPath: full.path)[.modificationDate] as? Date) ?? .distantPast
        if fullDate.timeIntervalSince(minDate) > 30 {
            return full
        }
        return min
    }

    static var liveURL: URL {
        let env = ProcessInfo.processInfo.environment
        if let raw = env["LOCAL_AI_MONITOR_LIVE"], !raw.isEmpty {
            return URL(fileURLWithPath: (raw as NSString).expandingTildeInPath)
        }
        if let st = env["LOCAL_AI_MONITOR_STATE"], !st.isEmpty {
            let dir = URL(fileURLWithPath: (st as NSString).expandingTildeInPath, isDirectory: true)
            return newestLiveURL(in: dir)
        }
        // Production lean: prefer min feed from C plane when present.
        return newestLiveURL(in: defaultProductionState)
    }

    static var pausedURL: URL {
        stateDir.appendingPathComponent("paused")
    }

    /// Never show "lab" or raw filenames on the product surface.
    static var labChipSuffix: String { "" }

    static var missingLiveHint: String {
        return "Monitor monitor is not updating."
    }
}

// MARK: - Model

@MainActor
final class LiveModel: ObservableObject {
    @Published var titleText: String = "\(Brand.chip) · —"
    @Published var sentence: String = "Starting…"
    @Published var memoryLine: String = ""
    @Published var toolRows: [ToolRow] = []
    @Published var buzzProjects: [BuzzProjectRow] = []
    @Published var allSessions: [SessionRow] = []
    @Published var nav: NavLevel = .home
    @Published var isStale: Bool = true
    @Published var isPaused: Bool = false
    @Published var focusNote: String = ""
    @Published var editToolRows: [EditToolRow] = []
    @Published var showIdleInstalled: Bool = true
    @Published var catalogKnown: [LiveSnap.ToolsMeta.KnownTool] = []
    @Published var attentionRows: [AttentionRow] = []
    @Published var budgets: [String: LiveSnap.BudgetItem] = [:]
    @Published var canResumeSomething: Bool = false
    /// Tool id pre-selected for New session (empty = pick in sheet)
    @Published var newSessionToolId: String = ""
    /// Top live sessions by RSS (home “Heaviest sessions”).
    @Published var heaviestSessions: [SessionRow] = []
    /// One-line preview for the largest-load action (matches end_heaviest argmax).
    @Published var endHeavyPreview: String = ""
    /// Free-RAM interrupt (local-ai-rm) — only when band warn/hard.
    @Published var resourceShow: Bool = false
    @Published var resourceBand: String = "ok"
    @Published var resourceTitle: String = ""
    @Published var resourceDetail: String = ""
    @Published var resourceCandidateLabel: String = ""
    @Published var resourceActionLabel: String = "Pause unused"
    @Published var resourceCandidateApp: String = ""
    @Published var resourceCandidateSessionId: String = ""
    @Published var resourceChip: String = ""
    @Published var resourceManagedLabel: String = ""
    @Published var resourceAutoEnd: Bool = false
    @Published var resourcePressureState: String = "unknown"
    @Published var resourceRecommendation: String = "refuse"
    @Published var resourceCanStartHeavy: Bool = true
    @Published var resourceCheckpointHint: String = ""
    @Published var resourceBrowserHint: String = ""
    @Published var resourceStateLabel: String = "Unknown"
    @Published var resourceStateDetail: String = ""
    @Published var resourceHeadroomMb: Int?
    @Published var resourceHeadroomOkMb: Int?
    @Published var resourceHeadroomWarnMb: Int?
    @Published var resourceMemsizeMb: Int?
    @Published var resourceAiMemPct: Double?
    @Published var resourceCpuCount: Int?
    @Published var resourceCpuCapacityPct: Double?
    @Published var resourceProfileStatus: String = "unknown"
    @Published var resourceProfileConfidence: Double?
    @Published var resourceSwapUsedMb: Int?
    @Published var resourceSwapTotalMb: Int?
    @Published var resourceThrashScore: Double?
    @Published var resourceAiMb: Int?
    @Published var resourceHeavyRssKb: Int?
    @Published var resourceLearnedAiRssMb: Double?
    @Published var resourceLearnedAiMemPct: Double?
    @Published var resourceLearnedCpuCapacityPct: Double?
    @Published var resourceLearnedThrashScore: Double?
    @Published var parkingMessage: String = "Loading Parking Lot…"
    @Published var parkingDetail: String = ""
    @Published var parkingParkable: [ParkingItem] = []
    @Published var parkingProtected: [ParkingItem] = []
    @Published var parkingAlreadyParked: [ParkingItem] = []
    @Published var parkingCanApply: Bool = false
    @Published var browserMessage: String = "Checking browsers…"
    @Published var browserDetail: String = ""
    @Published var browserFrontmost: String = ""
    @Published var browserCandidates: [BrowserItem] = []
    @Published var browserProtected: [BrowserItem] = []
    /// Tool ids the user has quarantined (stay off until allowed).
    @Published var quarantinedTools: Set<String> = []

    private var timer: Timer?
    private let refreshS: TimeInterval = 3.0
    /// Age > factor × sample_interval → stale. Lean C feed loops at 10–15s;
    /// keep factor generous so brief hiccups do not freeze the glass UI.
    private let staleFactor: Double = 6.0
    private var lastToolsMeta: LiveSnap.ToolsMeta?
    /// tool id → last epoch when non-idle (for recency sort).
    private var toolLastActive: [String: TimeInterval] = [:]
    private var sparksByApp: [String: [Double]] = [:]
    private var activityLoaded = false
    private var lastDebugLog: Date = .distantPast

    private var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    private var livePath: URL { LivePathConfig.liveURL }

    private var pausedPath: URL { LivePathConfig.pausedURL }

    private var toolsPrefsPath: URL {
        home.appendingPathComponent(".config/local-ai-monitor/tools.json")
    }

    private var toolActivityPath: URL {
        home.appendingPathComponent(".config/local-ai-monitor/tool_activity.json")
    }

    init() {
        start()
    }

    var selectedToolTitle: String {
        if case .tool(let id) = nav { return Human.toolName(id) }
        return ""
    }

    /// Menu bar chip (locked style D): text only when fine; orange ! only on interrupt.
    var chipSymbolName: String? {
        if isStale || isPaused { return "exclamationmark.circle.fill" }
        if attentionRows.contains(where: { $0.isNeedsYou || $0.isLimited }) {
            return "exclamationmark.circle.fill"
        }
        if resourceShow && (resourcePressureState == "stop_start_gate" || resourcePressureState == "freeze_risk" || resourcePressureState == "unknown") {
            return "exclamationmark.circle.fill"
        }
        return nil
    }

    var resourceTint: Color {
        switch resourcePressureState {
        case "calibrating": return .blue
        case "ok": return .green
        case "caution": return .yellow
        case "stop_start_gate": return .orange
        case "freeze_risk": return .red
        default: return resourceBand == "hard" ? .red : (resourceBand == "warn" ? .orange : .secondary)
        }
    }

    var resourceIcon: String {
        switch resourcePressureState {
        case "calibrating": return "gauge.medium"
        case "ok": return "checkmark.shield.fill"
        case "caution": return "gauge.medium"
        case "stop_start_gate": return "hand.raised.fill"
        case "freeze_risk": return "exclamationmark.triangle.fill"
        default: return "questionmark.circle.fill"
        }
    }

    var dynamicHeavyRssKb: Int {
        let memMb = resourceMemsizeMb ?? Int(MonitorCopy.physicalMemoryMb)
        let floorKb = LoadBand.heavyRssFloorKb
        let ceilingKb = 4096 * 1024
        let scaledKb = Int(Double(max(memMb, 0)) * 1024.0 * 0.08)
        var threshold = min(ceilingKb, max(floorKb, scaledKb))
        if let kb = resourceHeavyRssKb, kb > 0 {
            threshold = min(ceilingKb, max(threshold, kb))
        }
        if let learnedMb = resourceLearnedAiRssMb, learnedMb > 0 {
            let learnedKb = Int(learnedMb * 1024.0 * 1.35)
            threshold = min(ceilingKb, max(threshold, learnedKb))
        }
        return threshold
    }

    func isDynamicallyHeavy(_ row: SessionRow) -> Bool {
        if resourcePressureState == "stop_start_gate" || resourcePressureState == "freeze_risk" || !resourceCanStartHeavy {
            return row.rssKb >= 512 * 1024 || row.cpu >= 50.0
        }
        return row.rssKb >= dynamicHeavyRssKb || row.cpu >= 80.0
    }

    func needsEndConfirm(_ row: SessionRow) -> Bool {
        row.isMultiPid || isDynamicallyHeavy(row)
    }

    var endLoadTitle: String {
        guard let top = heaviestSessions.first else { return "End largest" }
        return isDynamicallyHeavy(top) ? "End heavy" : "End largest"
    }

    func loadBadgeTitle(for row: SessionRow, index: Int) -> String? {
        guard index == 0 else { return nil }
        return isDynamicallyHeavy(row) ? "End heavy" : "Largest"
    }

    func sessionsForSelectedTool() -> [SessionRow] {
        guard case .tool(let id) = nav else { return [] }
        return allSessions
            .filter { $0.app == id }
            .sorted { a, b in
                // Heaviest first, then CPU; matches the largest-load action.
                if a.rssKb != b.rssKb { return a.rssKb > b.rssKb }
                return a.cpu > b.cpu
            }
    }

    func start() {
        if timer != nil {
            reload()
            return
        }
        debugLog("start live=\(livePath.path)", force: true)
        loadToolActivity()
        isPaused = FileManager.default.fileExists(atPath: pausedPath.path)
        reload()
        timer = Timer.scheduledTimer(withTimeInterval: refreshS, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.reload() }
        }
        if let t = timer { RunLoop.main.add(t, forMode: .common) }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    func goHome() {
        nav = .home
        focusNote = ""
    }

    func openTool(_ appId: String) {
        nav = .tool(appId)
        focusNote = "Tap a session to open it."
    }

    func openEditTools() {
        nav = .editTools
        focusNote = "Turn off tools you do not want to see."
        rebuildEditRows()
    }

    func openNewSession(preselect toolId: String = "") {
        newSessionToolId = toolId
        nav = .newSession
        focusNote = resourceCanStartHeavy
            ? "Start a new chat in Terminal or the app."
            : "RAM/swap gate is closed — wait or park background load first."
    }

    func launchNewSession(toolId: String) {
        if !resourceCanStartHeavy {
            focusNote = "Not starting \(Human.toolName(toolId)): RAM/swap gate is closed."
            return
        }
        focusNote = LaunchHelper.launch(toolId: toolId, cwd: nil)
    }

    func openMore() {
        nav = .more
        focusNote = ""
        loadBrowserCleanup()
        loadParkingLot()
    }

    func openParkingLot() {
        nav = .parkingLot
        focusNote = ""
        loadParkingLot()
    }

    func openBrowserCleanup() {
        nav = .browserCleanup
        focusNote = ""
        loadBrowserCleanup()
    }

    /// Resume after limit — continue the same Claude/Grok conversation when possible.
    func resumeReadyWork() {
        if let row = attentionRows.first(where: { $0.canResume }) {
            resumeWork(row)
            return
        }
        focusNote = "Nothing is ready to resume yet."
    }

    func resumeWork(_ row: AttentionRow) {
        focusNote = LaunchHelper.launch(
            toolId: row.app,
            cwd: row.cwd,
            sessionId: row.sessionId,
            resume: true
        )
        // Keep note visible longer — user often looks at Terminal first.
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [weak self] in
            if self?.focusNote.hasPrefix("Started") == true
                || self?.focusNote.hasPrefix("Could not") == true {
                self?.focusNote = ""
            }
        }
    }

    func openSession(_ row: SessionRow) {
        if !row.openable {
            focusNote = row.openDenied.isEmpty
                ? "Cannot open — no window for this session"
                : row.openDenied
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                if self?.focusNote == row.openDenied || self?.focusNote.hasPrefix("Cannot open") == true {
                    self?.focusNote = ""
                }
            }
            return
        }
        let result = FocusHelper.open(row)
        focusNote = result.message
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) { [weak self] in
            if self?.focusNote == result.message {
                self?.focusNote = ""
            }
        }
    }

    func isQuarantined(_ toolId: String) -> Bool {
        quarantinedTools.contains(toolId)
    }

    func quarantineTool(_ toolId: String) {
        let name = Human.toolName(toolId)
        let ok = EndConfirm.ask(
            title: "Quarantine \(name)?",
            message: "Stops \(name) now and keeps it from running in the background until you allow it again.\n\nUse this for tools you are not using (for example OpenClaw gateway)."
        )
        if !ok { return }
        focusNote = "Quarantining \(name)…"
        focusNote = QuarantineHelper.add(toolId)
        quarantinedTools.insert(toolId)
        afterEndUI(removedApp: toolId, removedSession: nil, note: focusNote)
    }

    func unquarantineTool(_ toolId: String) {
        focusNote = QuarantineHelper.remove(toolId)
        quarantinedTools.remove(toolId)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.reload()
        }
    }

    /// End one session (SIGTERM → SIGKILL) via `local-ai-monitor end-session`.
    func endSession(_ row: SessionRow) {
        guard let sid = row.sessionId, !sid.isEmpty else {
            focusNote = "No session id — cannot end."
            return
        }
        if needsEndConfirm(row) {
            let procNote = row.isMultiPid ? "\(row.nproc) processes" : "1 process"
            let ok = EndConfirm.ask(
                title: "End “\(row.title)”?",
                message: "\(Human.toolName(row.app)) · \(row.mem) · \(procNote).\nThis frees RAM and stops the session."
            )
            if !ok { return }
        }
        performEndSession(row)
    }

    /// End every live session for the selected tool.
    func endAllForSelectedTool() {
        guard case .tool(let id) = nav else { return }
        let rows = allSessions.filter { $0.app == id }
        let totalRss = rows.map(\.rssKb).reduce(0, +)
        let totalProc = rows.map(\.nproc).reduce(0, +)
        let heavy = totalRss >= dynamicHeavyRssKb || totalProc > 1 || rows.count > 1
        if heavy {
            let ok = EndConfirm.ask(
                title: "End all \(Human.toolName(id)) sessions?",
                message: "\(rows.count) session(s) · \(Human.memShort(totalRss)) · \(totalProc) process(es).\nThis cannot be undone from here."
            )
            if !ok { return }
        }
        performEndAll(tool: id)
    }

    /// End the single highest-RSS session across all tools.
    func endHeaviest() {
        guard let top = heaviestSessions.first else {
            focusNote = "Nothing to end."
            return
        }
        let dynamicHeavy = isDynamicallyHeavy(top)
        if needsEndConfirm(top) {
            let procNote = top.isMultiPid ? "\(top.nproc) processes" : "1 process"
            let ok = EndConfirm.ask(
                title: dynamicHeavy ? "End heavy session?" : "End largest session?",
                message: dynamicHeavy
                    ? "“\(top.title)” · \(Human.toolName(top.app)) · \(top.mem) · \(procNote).\nThis is above this Mac's current heavy-load threshold."
                    : "“\(top.title)” · \(Human.toolName(top.app)) · \(top.mem) · \(procNote).\nThis is the largest current AI load, not a heavy-load warning for this Mac."
            )
            if !ok { return }
        }
        performEndHeaviest()
    }

    /// Optimistic remove + fast multi-reload so the panel does not lag 10s.
    private func afterEndUI(removedApp: String? = nil, removedSession: String? = nil, note: String) {
        focusNote = note
        if let app = removedApp, let sid = removedSession, !sid.isEmpty {
            allSessions.removeAll { $0.app == app && $0.sessionId == sid }
            heaviestSessions.removeAll { $0.app == app && $0.sessionId == sid }
            if removedApp == "OpenClaw" {
                // Gateway is a LaunchAgent — expect it gone after bootout
                allSessions.removeAll { $0.app == "OpenClaw" }
                heaviestSessions.removeAll { $0.app == "OpenClaw" }
                toolRows.removeAll { $0.id == "OpenClaw" }
            }
            refreshHeaviest(from: allSessions)
        } else if let app = removedApp {
            allSessions.removeAll { $0.app == app }
            heaviestSessions.removeAll { $0.app == app }
            toolRows.removeAll { $0.id == app }
            refreshHeaviest(from: allSessions)
        }
        // Immediate + staggered reloads (live.json rewritten by end-session)
        reload()
        for delay in [0.35, 1.0, 2.5] as [Double] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                self?.reload()
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            if self?.focusNote == note || self?.focusNote.hasPrefix("Ended") == true
                || self?.focusNote.hasPrefix("Freeing") == true
                || self?.focusNote.contains("stopped") == true {
                self?.focusNote = ""
            }
        }
    }

    private func performEndSession(_ row: SessionRow) {
        guard let sid = row.sessionId, !sid.isEmpty else {
            focusNote = "No session id — cannot end."
            return
        }
        focusNote = "Ending \(row.title)…"
        let msg = EndSessionHelper.end(tool: row.app, sessionId: sid)
        afterEndUI(removedApp: row.app, removedSession: sid, note: msg)
    }

    private func performEndAll(tool id: String) {
        focusNote = "Ending all \(Human.toolName(id))…"
        let msg = EndSessionHelper.endAll(tool: id)
        afterEndUI(removedApp: id, removedSession: nil, note: msg)
    }

    private func performEndHeaviest() {
        let top = heaviestSessions.first
        focusNote = "Ending \(endLoadTitle.lowercased()) session…"
        let msg = EndSessionHelper.endHeaviest()
        afterEndUI(
            removedApp: top?.app,
            removedSession: top?.sessionId,
            note: msg
        )
    }

    /// Reclaim idle from resource strip — ends policy candidate (confirm).
    func freeRamFromResource() {
        let app = resourceCandidateApp
        let sid = resourceCandidateSessionId
        let label = resourceCandidateLabel.isEmpty ? "heaviest idle AI session" : resourceCandidateLabel
        guard !app.isEmpty, !sid.isEmpty else {
            focusNote = "No unused background work to pause right now."
            return
        }
        let ok = EndConfirm.ask(
            title: "Pause unused background work?",
            message: "This pauses \(label).\nYour chats and active work stay open.\nBackground helpers that restart themselves are fully stopped.\nRunway will not close mid-stream work."
        )
        if !ok { return }
        focusNote = "Pausing unused work…"
        let msg = EndSessionHelper.end(tool: app, sessionId: sid)
        afterEndUI(removedApp: app, removedSession: sid, note: msg)
    }

    func reload() {
        isPaused = FileManager.default.fileExists(atPath: pausedPath.path)
        guard FileManager.default.isReadableFile(atPath: livePath.path) else {
            debugLog("missing live=\(livePath.path)", force: true)
            applyStale(reason: LivePathConfig.missingLiveHint)
            return
        }
        do {
            let data = try Data(contentsOf: livePath)
            let snap = try JSONDecoder().decode(LiveSnap.self, from: data)
            apply(snap)
            debugLog("applied \(livePath.lastPathComponent) ts=\(snap.ts) title=\(titleText)")
        } catch {
            debugLog("decode failed \(livePath.path): \(error)", force: true)
            applyStale(reason: "Could not read monitor data (\(livePath.lastPathComponent)).")
        }
    }

    private func debugLog(_ message: String, force: Bool = false) {
        let now = Date()
        if !force && now.timeIntervalSince(lastDebugLog) < 15 { return }
        lastDebugLog = now
        let stamp = ISO8601DateFormatter().string(from: now)
        let path = LivePathConfig.stateDir.appendingPathComponent("menubar.log")
        let line = "\(stamp) \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        if FileManager.default.fileExists(atPath: path.path),
           let fh = try? FileHandle(forWritingTo: path) {
            defer { try? fh.close() }
            try? fh.seekToEnd()
            try? fh.write(contentsOf: data)
        } else {
            try? data.write(to: path, options: .atomic)
        }
    }

    private func apply(_ snap: LiveSnap) {
        let interval = snap.sample_interval_s ?? 10.0
        let age = ageSeconds(of: snap.ts)
        let stale = age.map { $0 > staleFactor * interval } ?? true
        isStale = stale || isPaused

        let sessions = snap.sessions ?? []
        allSessions = sessions.compactMap { makeSessionRow($0) }

        // Heaviest preview even when paused/stale (if we still have sessions)
        refreshHeaviest(from: allSessions)

        if isPaused {
            titleText = "\(Brand.chip) · Paused" + LivePathConfig.labChipSuffix
            sentence = "Monitoring is paused. Choose Resume when you want updates again."
            memoryLine = ""
            toolRows = []
            buzzProjects = []
            return
        }

        if stale {
            applyStale(reason: "The background monitor is not updating.")
            return
        }

        let cpu = snap.totals?.cpu_pct ?? 0
        let rss = snap.totals?.rss_kb ?? 0
        let topCpu = snap.top?.cpu_pct ?? 0
        titleText = Human.chip(cpu: cpu, stale: false)
        // Capacity chip wins over CPU% once Runway has a reading.
        // Attention (needs you / limited) still overrides later.
        sentence = Human.sentence(
            cpu: cpu,
            rssKb: rss,
            topApp: snap.top?.app,
            topCpu: topCpu,
            stale: false
        )
        // Product copy: one memory story. When the headroom card is visible,
        // keep swap/headroom there and avoid duplicate, slightly different totals.
        let resourceVisible = snap.resource != nil
        memoryLine = resourceVisible ? "" : "Using \(Human.memPhrase(rss))."
        if !resourceVisible, let r = snap.resource {
            var parts: [String] = []
            if let swapUsed = r.swap_used_mb {
                if let swapTotal = r.swap_total_mb, swapTotal > 0 {
                    parts.append("swap \(Human.mbShort(swapUsed)) / \(Human.mbShort(swapTotal))")
                } else {
                    parts.append("swap \(Human.mbShort(swapUsed))")
                }
            }
            if let thrash = r.thrash_score {
                parts.append(String(format: "thrash %.1f", thrash))
            }
            if !parts.isEmpty {
                memoryLine += " " + parts.joined(separator: " · ") + "."
            }
        }
        lastToolsMeta = snap.tools
        catalogKnown = snap.tools?.known ?? []
        showIdleInstalled = snap.tools?.show_idle_installed ?? true
        sparksByApp = snap.sparks ?? [:]
        noteToolActivity(from: sessions)
        buzzProjects = aggregateBuzz(sessions)
        attentionRows = (snap.attention ?? []).map { item in
            let kind = item.kind
            let ready = kind == "ready_to_resume"
                || (kind == "limited" && isPastReset(item.resets_at))
            return AttentionRow(
                id: item.id,
                app: item.app,
                kind: ready && kind == "limited" ? "ready_to_resume" : kind,
                title: item.title ?? Human.toolName(item.app),
                detail: item.detail ?? "",
                confidence: item.confidence ?? "low",
                resetsAt: item.resets_at,
                cwd: item.cwd,
                sessionId: item.session_id,
                canResume: ready || kind == "ready_to_resume"
            )
        }
        budgets = snap.budgets ?? [:]
        quarantinedTools = Set(snap.quarantine?.tools ?? [])
        // CCM / background log stays out of the menu bar (see `local-ai-monitor status`)
        canResumeSomething = attentionRows.contains { $0.canResume }

        // Resource policy (local-ai-rm) — RAM headroom / swap state.
        // Show the green state too; a dashboard is only trustworthy if "safe"
        // is visible before the Mac is already under pressure.
        if let r = snap.resource {
            resourceShow = true
            resourceBand = r.band ?? "warn"
            let pressure = r.pressure_state ?? (resourceBand == "hard" ? "freeze_risk" : (resourceBand == "warn" ? "stop_start_gate" : "ok"))
            let canStart = r.can_start_heavy ?? (pressure == "ok")
            resourcePressureState = pressure
            resourceCanStartHeavy = canStart
            resourceStateLabel = Human.pressureLabel(pressure, band: resourceBand)
            if let plainTitle = r.plain_title, !plainTitle.isEmpty {
                resourceTitle = plainTitle
            } else {
                resourceTitle = Human.pressureTitle(pressure, band: resourceBand, canStart: canStart)
            }
            resourceStateDetail = Human.pressureDetail(pressure, band: resourceBand, canStart: canStart)
            if let plain = r.plain_detail, !plain.isEmpty {
                resourceDetail = plain
            } else if let detail = r.detail, !detail.isEmpty {
                resourceDetail = detail
            } else {
                let summary = Human.resourceSummary(
                    headroomMb: r.headroom_mb,
                    swapUsedMb: r.swap_used_mb,
                    swapTotalMb: r.swap_total_mb,
                    thrash: r.thrash_score,
                    aiMb: r.ai_rss_mb
                )
                resourceDetail = summary.isEmpty ? "" : summary + "."
            }
            resourceCandidateLabel = r.candidate_label ?? ""
            resourceActionLabel = r.action_label ?? "Pause unused"
            resourceCandidateApp = r.candidate_app ?? ""
            resourceCandidateSessionId = r.candidate_session_id ?? ""
            resourceChip = "\(Brand.chip) · \(resourceStateLabel)"
            resourceManagedLabel = r.managed_label ?? ""
            resourceAutoEnd = r.auto_end ?? false
            resourceRecommendation = r.recommendation ?? "refuse"
            resourceCheckpointHint = r.checkpoint_hint ?? ""
            resourceBrowserHint = r.browser_hint ?? ""
            resourceHeadroomMb = r.headroom_mb
            resourceHeadroomOkMb = r.headroom_ok_mb
            resourceHeadroomWarnMb = r.headroom_warn_mb
            resourceMemsizeMb = r.memsize_mb
            resourceAiMemPct = r.ai_mem_pct
            resourceCpuCount = r.cpu_count
            resourceCpuCapacityPct = r.cpu_capacity_pct
            resourceProfileStatus = r.profile_status ?? "unknown"
            resourceProfileConfidence = r.profile_confidence
            resourceSwapUsedMb = r.swap_used_mb
            resourceSwapTotalMb = r.swap_total_mb
            resourceThrashScore = r.thrash_score
            resourceAiMb = r.ai_rss_mb
            resourceHeavyRssKb = r.heavy_rss_kb
            resourceLearnedAiRssMb = r.learned_ai_rss_mb
            resourceLearnedAiMemPct = r.learned_ai_mem_pct
            resourceLearnedCpuCapacityPct = r.learned_cpu_capacity_pct
            resourceLearnedThrashScore = r.learned_thrash_score
            // After self-manage recovery, still show brief success card
            if r.managed == true, let ml = r.managed_label, !ml.isEmpty {
                resourceShow = true
            }
        } else {
            resourceShow = false
            resourceBand = "ok"
            resourceTitle = ""
            resourceDetail = ""
            resourceCandidateLabel = ""
            resourceCandidateApp = ""
            resourceCandidateSessionId = ""
            resourceChip = ""
            resourceManagedLabel = ""
            resourceAutoEnd = false
            resourcePressureState = "ok"
            resourceRecommendation = "do_nothing"
            resourceCanStartHeavy = true
            resourceCheckpointHint = ""
            resourceBrowserHint = ""
            resourceStateLabel = "Safe"
            resourceStateDetail = ""
            resourceHeadroomMb = nil
            resourceHeadroomOkMb = nil
            resourceHeadroomWarnMb = nil
            resourceMemsizeMb = nil
            resourceAiMemPct = nil
            resourceCpuCount = nil
            resourceCpuCapacityPct = nil
            resourceProfileStatus = "unknown"
            resourceProfileConfidence = nil
            resourceSwapUsedMb = nil
            resourceSwapTotalMb = nil
            resourceThrashScore = nil
            resourceAiMb = nil
            resourceHeavyRssKb = nil
            resourceLearnedAiRssMb = nil
            resourceLearnedAiMemPct = nil
            resourceLearnedCpuCapacityPct = nil
            resourceLearnedThrashScore = nil
        }

        toolRows = aggregateTools(sessions, meta: snap.tools)
        refreshHeaviest(from: allSessions)

        // Chip: needs you > limited > Runway capacity > CPU%
        let needN = attentionRows.filter { $0.isNeedsYou && $0.confidence != "low" }.count
        let limitedN = attentionRows.filter { $0.isLimited }.count
        if needN > 0 {
            titleText = needN == 1 ? "\(Brand.chip) · Needs you" : "\(Brand.chip) · \(needN) need you"
        } else if limitedN > 0 {
            titleText = "\(Brand.chip) · Limited"
        } else if resourceShow, !resourceChip.isEmpty {
            titleText = resourceChip
        }
        titleText += LivePathConfig.labChipSuffix

        if resourceShow, resourcePressureState == "freeze_risk" {
            sentence = "This Mac is under strain. Your open work stays safe."
        } else if resourceShow, !resourceCanStartHeavy {
            sentence = "Do not open another heavy app yet. Your chats stay open."
        } else if resourceShow, resourcePressureState == "caution" {
            sentence = "You can still start more work. Keep an eye on this Mac."
        } else if resourceShow {
            sentence = "You can start more work."
        }

        if case .editTools = nav {
            rebuildEditRows()
        }

        // If navigated into a tool that disappeared, go home
        if case .tool(let id) = nav, !toolRows.contains(where: { $0.id == id }) {
            nav = .home
        }
    }

    private func refreshHeaviest(from sessions: [SessionRow]) {
        heaviestSessions = Array(
            sessions.sorted { a, b in
                if a.rssKb != b.rssKb { return a.rssKb > b.rssKb }
                return a.cpu > b.cpu
            }.prefix(3)
        )
        if let top = heaviestSessions.first {
            endHeavyPreview = isDynamicallyHeavy(top)
                ? "Ends heavy: \(top.title) · \(top.mem)"
                : "Largest load: \(top.title) · \(top.mem)"
        } else {
            endHeavyPreview = ""
        }
    }

    private func loadToolActivity() {
        guard !activityLoaded else { return }
        activityLoaded = true
        guard FileManager.default.isReadableFile(atPath: toolActivityPath.path),
              let data = try? Data(contentsOf: toolActivityPath),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        var map: [String: TimeInterval] = [:]
        for (k, v) in obj {
            if let d = v as? Double {
                map[k] = d
            } else if let n = v as? NSNumber {
                map[k] = n.doubleValue
            }
        }
        toolLastActive = map
    }

    private func saveToolActivity() {
        let dir = toolActivityPath.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(
            withJSONObject: toolLastActive, options: [.prettyPrinted, .sortedKeys]
        ) {
            try? data.write(to: toolActivityPath, options: .atomic)
        }
    }

    private func noteToolActivity(from sessions: [LiveSnap.Session]) {
        let now = Date().timeIntervalSince1970
        var changed = false
        var seen = Set<String>()
        for s in sessions {
            if s.alive == false { continue }
            guard s.cpu_pct >= 0.3 || s.rss_kb >= 8 * 1024 else { continue }
            seen.insert(s.app)
            let prev = toolLastActive[s.app] ?? 0
            // Throttle disk writes: only bump if ≥15s since last stamp
            if now - prev >= 15 {
                toolLastActive[s.app] = now
                changed = true
            }
        }
        if changed { saveToolActivity() }
        _ = seen
    }

    private func isPastReset(_ iso: String?) -> Bool {
        guard let iso, !iso.isEmpty else { return false }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: iso) { return Date() >= d }
        f.formatOptions = [.withInternetDateTime]
        if let d = f.date(from: iso) { return Date() >= d }
        return false
    }

    private func rebuildEditRows() {
        let prefs = loadToolPrefs()
        showIdleInstalled = prefs.show_idle_installed
        let known = catalogKnown
        if known.isEmpty {
            // Fallback static catalog when collector meta missing
            let fallback = [
                ("Claude Desktop", "Co-Work"),
                ("ChatGPT", "ChatGPT"),
                ("Buzz", "Buzz"),
                ("OpenClaw", "OpenClaw"),
                ("Codex", "Codex"),
                ("OpenAI CLI", "OpenAI CLI"),
                ("Cursor", "Cursor"),
                ("Grok", "Grok"),
                ("Claude CLI", "Anthropic CLI"),
            ]
            editToolRows = fallback.map { id, title in
                EditToolRow(
                    id: id,
                    title: title,
                    installed: lastToolsMeta?.installed?.contains(id) ?? false,
                    isShown: !prefs.hidden.contains(id)
                )
            }
            return
        }
        editToolRows = known.map { k in
            EditToolRow(
                id: k.id,
                title: k.display,
                installed: k.installed ?? false,
                isShown: !prefs.hidden.contains(k.id)
            )
        }
    }

    func loadToolPrefs() -> ToolPrefsFile {
        let url = toolsPrefsPath
        guard FileManager.default.isReadableFile(atPath: url.path),
              let data = try? Data(contentsOf: url),
              let prefs = try? JSONDecoder().decode(ToolPrefsFile.self, from: data)
        else {
            return .empty
        }
        return prefs
    }

    func saveToolPrefs(_ prefs: ToolPrefsFile) {
        let url = toolsPrefsPath
        let dir = url.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? enc.encode(prefs) {
            try? data.write(to: url, options: .atomic)
        }
        // Reflect immediately in view; next collector sample refreshes meta
        rebuildEditRows()
        reload()
    }

    func setToolShown(_ toolId: String, shown: Bool) {
        var prefs = loadToolPrefs()
        var set = Set(prefs.hidden)
        if shown {
            set.remove(toolId)
        } else {
            set.insert(toolId)
        }
        prefs.hidden = set.sorted()
        saveToolPrefs(prefs)
        focusNote = shown
            ? "\(Human.toolName(toolId)) will appear when installed or running."
            : "\(Human.toolName(toolId)) hidden from the list."
    }

    func setShowIdle(_ on: Bool) {
        var prefs = loadToolPrefs()
        prefs.show_idle_installed = on
        saveToolPrefs(prefs)
        showIdleInstalled = on
        focusNote = on
            ? "Installed tools show even when quiet."
            : "Only tools currently running appear."
    }

    private func makeSessionRow(_ s: LiveSnap.Session) -> SessionRow? {
        if s.alive == false { return nil }
        let state = ActivityState.from(cpu: s.cpu_pct, alive: s.alive)
        var title: String
        var subtitle: String
        if s.app == "Buzz" {
            let (ch, ag) = Human.parseBuzz(label: s.label, sessionId: s.session_id)
            title = Human.channelName(ch)
            subtitle = Human.agentName(ag)
        } else if s.app == "OpenClaw" {
            title = s.title_hint ?? "Gateway"
            subtitle = s.subtitle_hint ?? "Background service · no app window"
        } else if s.app == "Grok" {
            let lab = s.label ?? ""
            if lab.hasPrefix("pid") || lab.isEmpty {
                title = "Grok chat"
                if let d = s.detail, d.hasPrefix("/") {
                    subtitle = (d as NSString).lastPathComponent
                } else {
                    subtitle = "Running"
                }
            } else {
                let left = lab.components(separatedBy: " · ").first ?? lab
                title = Human.titleWords(left)
                subtitle = "Grok"
            }
        } else if s.app == "Claude CLI" {
            if let lab = s.label, !lab.isEmpty, !lab.hasPrefix("pid") {
                title = Human.titleWords(lab)
            } else {
                title = "Anthropic CLI chat"
            }
            subtitle = "Anthropic CLI"
        } else if s.app == "Claude Desktop" {
            if let lab = s.label, !lab.isEmpty, lab != "Claude Desktop" {
                title = Human.titleWords(lab)
            } else {
                title = "Co-Work"
            }
            subtitle = "Co-Work"
        } else if s.app == "Codex" {
            if let lab = s.label, !lab.isEmpty, !lab.hasPrefix("pid") {
                title = Human.titleWords(lab)
            } else {
                title = "Codex chat"
            }
            subtitle = "Codex"
        } else if s.app == "Cursor" {
            if let lab = s.label, !lab.isEmpty, !lab.hasPrefix("pid") {
                title = Human.titleWords(lab)
            } else {
                title = "Workspace"
            }
            subtitle = "Cursor"
        } else if s.app == "OpenAI CLI" {
            if let lab = s.label, !lab.isEmpty, !lab.hasPrefix("pid") {
                title = Human.titleWords(lab)
            } else {
                title = "OpenAI CLI"
            }
            subtitle = "OpenAI CLI"
        } else {
            title = Human.toolName(s.app)
            subtitle = s.label ?? ""
        }
        if let sh = s.subtitle_hint, !sh.isEmpty, s.app != "Buzz" {
            // Prefer collector surface subtitle for services
            if s.kind == "service" || s.app == "OpenClaw" {
                subtitle = sh
            }
        }
        let path: String? = {
            guard let d = s.detail, d.hasPrefix("/") else { return s.detail }
            return d
        }()
        let nproc: Int = {
            if let n = s.nproc, n > 0 { return n }
            if let p = s.pids, !p.isEmpty { return p.count }
            return 1
        }()
        // Fail closed: missing openable → not openable for services; else use field
        let kind = s.kind ?? (s.app == "OpenClaw" ? "service" : "unknown")
        let openable: Bool = {
            if let o = s.openable { return o }
            return kind != "service" && s.app != "OpenClaw"
        }()
        let openDenied = s.open_denied
            ?? (openable ? "" : "Cannot open — no window for this session")
        let activity = s.activity
            ?? "\(Human.memShort(s.rss_kb)) · \(state.rawValue)"
        return SessionRow(
            id: s.session_id ?? "\(s.app)-\(s.label ?? "")-\(s.cpu_pct)",
            app: s.app,
            title: title,
            subtitle: subtitle,
            state: state,
            detailPath: path,
            sessionId: s.session_id,
            label: s.label,
            cpu: s.cpu_pct,
            mem: Human.memShort(s.rss_kb),
            rssKb: s.rss_kb,
            nproc: nproc,
            openable: openable,
            openDenied: openDenied,
            kind: kind,
            activity: activity
        )
    }

    private func applyStale(reason: String) {
        isStale = true
        titleText = "\(Brand.chip) · —" + LivePathConfig.labChipSuffix
        sentence = reason
        memoryLine = ""
        toolRows = []
        buzzProjects = []
        allSessions = []
        attentionRows = []
        resourceShow = false
        // Keep heaviestSessions / endHeavyPreview if last sample had data
        canResumeSomething = false
    }

    private func aggregateTools(
        _ sessions: [LiveSnap.Session],
        meta: LiveSnap.ToolsMeta?
    ) -> [ToolRow] {
        var map: [String: (cpu: Double, rss: Int, n: Int)] = [:]
        for s in sessions {
            if s.alive == false { continue }
            let cur = map[s.app] ?? (0, 0, 0)
            map[s.app] = (cur.cpu + s.cpu_pct, cur.rss + s.rss_kb, cur.n + 1)
        }

        let hidden = Set(meta?.hidden ?? [])
        let visibleList = meta?.visible
        let visibleSet = visibleList.map { Set($0) }
        let installed = meta?.installed ?? []
        let showIdle = meta?.show_idle_installed ?? true
        let running = Set(meta?.running ?? Array(map.keys))

        // Idle installed tools the user still wants to see
        if showIdle {
            for app in installed where !running.contains(app) && map[app] == nil {
                if hidden.contains(app) { continue }
                if let v = visibleSet, !v.contains(app) { continue }
                map[app] = (0, 0, 0)
            }
        }

        let heaviestApp: String? = {
            sessions
                .filter { $0.alive != false }
                .max(by: { $0.rss_kb < $1.rss_kb })?
                .app
        }()

        let filtered = map.filter { app, v in
            if hidden.contains(app) { return false }
            if let vset = visibleSet, !vset.contains(app) {
                // still show if actively using resources (prefs lag)
                return v.cpu > 0 || v.rss > 0
            }
            return v.cpu > 0 || v.rss > 0 || (showIdle && installed.contains(app))
        }

        return filtered
            .map { app, v -> ToolRow in
                let name = Human.toolName(app)
                let load = Human.loadPhrase(v.cpu)
                let mem = Human.memShort(v.rss)
                let count = v.n
                let quiet = count == 0 && v.cpu <= 0 && v.rss <= 0
                // Mem never lives in detail — fixed trailing column (no wrap).
                let detail: String
                if quiet {
                    detail = "Installed · Quiet"
                } else {
                    let countLabel = count == 1 ? "1 sess" : "\(count) sess"
                    detail = "\(load) · \(countLabel)"
                }
                let band = LoadBand.from(
                    cpu: v.cpu,
                    rssKb: v.rss,
                    sessionCount: count,
                    heavyRssKb: dynamicHeavyRssKb
                )
                return ToolRow(
                    id: app,
                    title: name,
                    detail: detail,
                    mem: quiet ? "—" : mem,
                    sessionCount: count,
                    cpu: v.cpu,
                    rssKb: v.rss,
                    isQuietInstalled: quiet,
                    loadBand: band,
                    spark: sparksByApp[app] ?? [],
                    ownsHeaviest: heaviestApp == app && !quiet
                )
            }
            .sorted { a, b in
                // 1) quiet installed last
                if a.isQuietInstalled != b.isQuietInstalled {
                    return !a.isQuietInstalled
                }
                // 2) heaviest RSS
                if a.rssKb != b.rssKb { return a.rssKb > b.rssKb }
                // 3) CPU
                if a.cpu != b.cpu { return a.cpu > b.cpu }
                // 4) recent activity
                let ta = toolLastActive[a.id] ?? 0
                let tb = toolLastActive[b.id] ?? 0
                if ta != tb { return ta > tb }
                return a.title < b.title
            }
    }

    private func aggregateBuzz(_ sessions: [LiveSnap.Session]) -> [BuzzProjectRow] {
        var groups: [String: [SessionRow]] = [:]
        for s in sessions where s.app == "Buzz" {
            guard let row = makeSessionRow(s) else { continue }
            let (ch, _) = Human.parseBuzz(label: s.label, sessionId: s.session_id)
            groups[ch, default: []].append(row)
        }
        return groups
            .sorted { a, b in
                a.value.map(\.cpu).reduce(0, +) > b.value.map(\.cpu).reduce(0, +)
            }
            .map { ch, rows in
                let sorted = rows.sorted { $0.cpu > $1.cpu }
                return BuzzProjectRow(
                    id: ch,
                    title: Human.channelName(ch),
                    workers: sorted.map {
                        BuzzWorkerRow(id: $0.id, title: $0.subtitle, session: $0)
                    }
                )
            }
    }

    private func ageSeconds(of ts: String) -> Double? {
        // 1) ISO8601 with/without fractional seconds
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = iso.date(from: ts) { return Date().timeIntervalSince(d) }
        iso.formatOptions = [.withInternetDateTime]
        if let d = iso.date(from: ts) { return Date().timeIntervalSince(d) }
        // 2) Normalize -0500 → -05:00 (common C strftime %z)
        var normalized = ts
        if ts.count >= 5 {
            let idx = ts.index(ts.endIndex, offsetBy: -5)
            let tail = String(ts[idx...])
            if (tail.first == "+" || tail.first == "-"),
               tail.count == 5,
               !tail.contains(":") {
                let sign = tail.prefix(1)
                let hh = tail.dropFirst().prefix(2)
                let mm = tail.suffix(2)
                normalized = String(ts[..<idx]) + "\(sign)\(hh):\(mm)"
                if let d = iso.date(from: normalized) {
                    return Date().timeIntervalSince(d)
                }
            }
        }
        // 3) Local wall time without offset
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        df.timeZone = TimeZone.current
        if let d = df.date(from: String(ts.prefix(19))) {
            return Date().timeIntervalSince(d)
        }
        // 4) File mtime fallback — never freeze UI if feed file is fresh
        if let attrs = try? FileManager.default.attributesOfItem(atPath: livePath.path),
           let m = attrs[.modificationDate] as? Date {
            return Date().timeIntervalSince(m)
        }
        return nil
    }

    func openSimpleList() {
        openResource(name: "open-simple", ext: "command", fallbacks: [
            home.appendingPathComponent(".local/lib/local-ai-monitor/menubar/scripts/open-simple.command"),
            home.appendingPathComponent(".local/lib/local-ai-monitor/menubar/dist/Local AI Monitor Menu.app/Contents/Resources/open-simple.command"),
        ])
    }

    func openExpertView() {
        openResource(name: "open-dash", ext: "command", fallbacks: [
            home.appendingPathComponent(".local/lib/local-ai-monitor/menubar/scripts/open-dash.command"),
            home.appendingPathComponent(".local/lib/local-ai-monitor/menubar/dist/Local AI Monitor Menu.app/Contents/Resources/open-dash.command"),
        ])
    }

    func loadParkingLot() {
        parkingMessage = "Checking safe background helpers…"
        parkingDetail = ""
        parkingParkable = []
        parkingProtected = []
        parkingAlreadyParked = []
        parkingCanApply = false
        if let snap = ParkingLotHelper.status() {
            applyParkingSnap(snap)
        } else {
            parkingMessage = "Parking Lot unavailable."
            parkingDetail = "Monitor could not read helper status."
        }
    }

    func applyParkingLot() {
        guard parkingCanApply else {
            focusNote = "Nothing safe to park right now."
            return
        }
        let ok = EndConfirm.ask(
            title: "Park background helpers?",
            message: "Monitor will lower priority for \(parkingParkable.count) idle helper(s).\nIt will not close AI sessions, Safari, Mail, or active work."
        )
        if !ok { return }
        focusNote = "Parking background helpers…"
        if let snap = ParkingLotHelper.apply() {
            applyParkingSnap(snap)
            focusNote = snap.message ?? "Parking Lot updated."
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                if self?.focusNote == snap.message {
                    self?.focusNote = ""
                }
            }
        } else {
            focusNote = "Parking Lot failed."
        }
        reload()
    }

    private func applyParkingSnap(_ snap: ParkingSnap) {
        parkingMessage = snap.message ?? "Parking Lot"
        parkingDetail = snap.detail ?? ""
        parkingParkable = snap.parkable ?? []
        parkingProtected = snap.protected ?? []
        parkingAlreadyParked = snap.already_parked ?? snap.parked ?? []
        parkingCanApply = !parkingParkable.isEmpty
    }

    func browserAdvice() {
        openBrowserCleanup()
    }

    func loadBrowserCleanup() {
        browserMessage = "Checking browsers…"
        browserDetail = ""
        browserFrontmost = ""
        browserCandidates = []
        browserProtected = []
        if let snap = BrowserCleanupHelper.status() {
            browserMessage = snap.message ?? "Browser Cleanup"
            browserDetail = snap.detail ?? ""
            browserFrontmost = snap.frontmost ?? ""
            browserCandidates = snap.candidates ?? []
            browserProtected = snap.protected ?? []
        } else {
            browserMessage = "Browser cleanup unavailable."
            browserDetail = "Monitor could not read browser process load."
        }
    }

    func closeCleanupApp(_ item: BrowserItem) {
        guard let app = item.app, !app.isEmpty else { return }
        let ok = EndConfirm.ask(
            title: "Close \(app)?",
            message: "Monitor will ask \(app) to quit gracefully.\nUse this only if you are done with it.",
            actionTitle: "Close"
        )
        if !ok { return }
        focusNote = "Closing \(app)…"
        _ = BrowserCleanupHelper.close(app: app)
        loadBrowserCleanup()
        reload()
    }

    func closeAllCleanupApps() {
        let count = browserCandidates.filter { $0.closeable ?? true }.count
        guard count > 0 else {
            focusNote = "No inactive apps to close."
            return
        }
        let ok = EndConfirm.ask(
            title: "Close \(count) inactive app\(count == 1 ? "" : "s")?",
            message: "Monitor will ask each inactive app to quit gracefully.\nActive/frontmost apps are skipped.",
            actionTitle: "Close"
        )
        if !ok { return }
        focusNote = "Closing inactive apps…"
        _ = BrowserCleanupHelper.closeAll()
        loadBrowserCleanup()
        reload()
    }

    private func openResource(name: String, ext: String, fallbacks: [URL]) {
        if let url = Bundle.main.url(forResource: name, withExtension: ext) {
            NSWorkspace.shared.open(url)
            return
        }
        for u in fallbacks where FileManager.default.fileExists(atPath: u.path) {
            NSWorkspace.shared.open(u)
            return
        }
    }

    func pauseMonitoring() {
        let dir = pausedPath.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: pausedPath.path, contents: Data("1\n".utf8))
        isPaused = true
        reload()
    }

    func resumeMonitoring() {
        try? FileManager.default.removeItem(at: pausedPath)
        isPaused = false
        reload()
    }
}

// MARK: - Layout metrics

enum PanelMetrics {
    static let minWidth: CGFloat = 320
    static let idealWidth: CGFloat = 380
    static let maxWidth: CGFloat = 500
    static let corner: CGFloat = 20
    static let cardCorner: CGFloat = 12
    static let stack: CGFloat = 8
    static let cardPad: CGFloat = 10
    /// Fixed column for mem labels — never let "429 MB" wrap.
    static let memCol: CGFloat = 56

    static var maxHeight: CGFloat {
        let screenH = NSScreen.main?.visibleFrame.height ?? 800
        return min(860, screenH * 0.90)
    }
}

enum MonitorTheme {
    static let snow = Color(red: 1.000, green: 1.000, blue: 0.994)
    static let pearl = Color(red: 0.996, green: 0.992, blue: 0.978)
    static let porcelain = Color(red: 0.990, green: 0.984, blue: 0.960)
    static let ivory = Color(red: 0.982, green: 0.968, blue: 0.925)
    static let inkText = Color(red: 0.015, green: 0.055, blue: 0.120)
    static let inkBlue = Color(red: 0.010, green: 0.050, blue: 0.105)
    static let sapphire = Color(red: 0.020, green: 0.205, blue: 0.420)
    static let platinum = Color(red: 0.905, green: 0.955, blue: 0.985)
    static let water = Color(red: 0.000, green: 0.500, blue: 0.760)
    static let sky = Color(red: 0.440, green: 0.780, blue: 1.000)
    static let rose = Color(red: 1.00, green: 0.70, blue: 0.78)
    static let mint = Color(red: 0.18, green: 0.66, blue: 0.45)
    static let amber = Color(red: 0.95, green: 0.54, blue: 0.16)
    static let gold = Color(red: 0.900, green: 0.690, blue: 0.330)
    static let champagne = Color(red: 0.965, green: 0.825, blue: 0.520)
    static let mutedText = Color(red: 0.070, green: 0.175, blue: 0.285)
    static let quietText = Color(red: 0.115, green: 0.300, blue: 0.455)
    static let cyan = water

    static let panelFill = LinearGradient(
        colors: [
            snow.opacity(0.995),
            Color(red: 0.992, green: 0.998, blue: 1.000).opacity(0.940),
            pearl.opacity(0.820)
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    static let cardFill = LinearGradient(
        colors: [
            snow.opacity(0.960),
            Color(red: 0.992, green: 0.998, blue: 1.000).opacity(0.760),
            porcelain.opacity(0.520)
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    static let controlFill = LinearGradient(
        colors: [
            snow.opacity(0.900),
            Color(red: 0.975, green: 0.995, blue: 1.000).opacity(0.620),
            champagne.opacity(0.090)
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    static let warmFill = LinearGradient(
        colors: [
            snow.opacity(0.800),
            champagne.opacity(0.240),
            rose.opacity(0.10)
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    static let hairline = Color.white.opacity(0.82)
    static let darkHairline = champagne.opacity(0.36)
    static let footerFill = LinearGradient(
        colors: [
            snow.opacity(0.92),
            Color(red: 0.930, green: 0.982, blue: 1.000).opacity(0.62),
            champagne.opacity(0.16)
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )
}

/// Monospaced mem cell: single line, fixed width, never wraps.
struct MemLabel: View {
    let text: String
    var tint: Color = .primary
    var weight: Font.Weight = .semibold
    var size: Font = .caption

    var body: some View {
        Text(text)
            .font(size.monospacedDigit().weight(weight))
            .foregroundStyle(tint)
            .lineLimit(1)
            .minimumScaleFactor(0.75)
            .multilineTextAlignment(.trailing)
            .frame(width: PanelMetrics.memCol, alignment: .trailing)
            .layoutPriority(2)
            .accessibilityLabel(text)
    }
}

// MARK: - Liquid Glass chrome (macOS Tahoe 26)

struct GlassSectionLabel: View {
    let title: String
    let systemImage: String

    var body: some View {
        Label(title, systemImage: systemImage)
            .font(.caption.weight(.semibold))
            .foregroundStyle(MonitorTheme.quietText)
            .labelStyle(.titleAndIcon)
            .symbolRenderingMode(.hierarchical)
    }
}

struct GlassCard<Content: View>: View {
    var interactive: Bool = false
    var compact: Bool = false
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .foregroundStyle(MonitorTheme.inkText)
            .padding(compact ? 8 : PanelMetrics.cardPad)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(MonitorTheme.cardFill, in: .rect(cornerRadius: PanelMetrics.cardCorner))
            .overlay {
                RoundedRectangle(cornerRadius: PanelMetrics.cardCorner, style: .continuous)
                    .stroke(MonitorTheme.darkHairline, lineWidth: 1)
            }
            .glassEffect(
                interactive ? .regular.interactive() : .regular,
                in: .rect(cornerRadius: PanelMetrics.cardCorner)
            )
    }
}

/// Compact glass control for the footer toolbar (icon + short label).
struct GlassToolbarItem: View {
    let title: String
    let systemImage: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 3) {
                Image(systemName: systemImage)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(MonitorTheme.sapphire)
                    .symbolRenderingMode(.hierarchical)
                    .frame(height: 18)
                Text(title)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(MonitorTheme.sapphire)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
            .padding(.horizontal, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(MonitorTheme.inkText)
        .background(MonitorTheme.footerFill, in: .rect(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(MonitorTheme.champagne.opacity(0.42), lineWidth: 1)
        }
        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 10))
        .accessibilityLabel(title)
    }
}

// MARK: - Panel

struct LocalAIMonitorPanel: View {
    @ObservedObject var model: LiveModel

    var body: some View {
        VStack(alignment: .leading, spacing: PanelMetrics.stack) {
            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: PanelMetrics.stack) {
                    switch model.nav {
                    case .home:
                        homeContent
                    case .tool:
                        toolDetailContent
                    case .editTools:
                        editToolsContent
                    case .newSession:
                        newSessionContent
                    case .parkingLot:
                        parkingLotContent
                    case .browserCleanup:
                        browserCleanupContent
                    case .more:
                        moreContent
                    }
                }
            }
            .frame(maxHeight: PanelMetrics.maxHeight)

            // Footer is part of the glass shell — not a free-floating strip
            footerBar
        }
        .padding(10)
        .frame(
            minWidth: PanelMetrics.minWidth,
            idealWidth: PanelMetrics.idealWidth,
            maxWidth: PanelMetrics.maxWidth,
            alignment: .topLeading
        )
        .fixedSize(horizontal: false, vertical: true)
        .foregroundStyle(MonitorTheme.inkText)
        .background(MonitorTheme.panelFill, in: .rect(cornerRadius: PanelMetrics.corner))
        .glassEffect(.regular, in: .rect(cornerRadius: PanelMetrics.corner))
        .preferredColorScheme(.light)
        .environment(\.colorScheme, .light)
        .padding(4)
        .onAppear { model.start() }
    }

    /// Phase 2: ≤3 primary controls. Utilities live under More.
    private var footerBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !model.focusNote.isEmpty {
                Text(model.focusNote)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(MonitorTheme.quietText)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            HStack(spacing: 6) {
                if model.nav != .home {
                    GlassToolbarItem(title: "Back", systemImage: "chevron.left", action: model.goHome)
                        .frame(maxWidth: .infinity)
                }
                GlassToolbarItem(title: "New", systemImage: "plus.circle.fill") {
                    if case .tool(let id) = model.nav {
                        model.openNewSession(preselect: id)
                    } else {
                        model.openNewSession()
                    }
                }
                .frame(maxWidth: .infinity)
                if model.canResumeSomething {
                    GlassToolbarItem(title: "Resume", systemImage: "play.fill", action: model.resumeReadyWork)
                        .frame(maxWidth: .infinity)
                }
                GlassToolbarItem(title: model.endLoadTitle, systemImage: "flame.fill", action: model.endHeaviest)
                    .frame(maxWidth: .infinity)
                    .help(model.endHeavyPreview.isEmpty ? "End largest session" : model.endHeavyPreview)
                    .accessibilityHint(model.endHeavyPreview)
                GlassToolbarItem(title: "More", systemImage: "ellipsis.circle", action: model.openMore)
                    .frame(maxWidth: .infinity)
            }
            // No second "Ends: …" line — target is marked once on heaviest row #1
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: PanelMetrics.cardCorner))
    }

    // MARK: Home

    @ViewBuilder
    private var homeContent: some View {
        headerCard
        // Host free-RAM interrupt first (physics) — zero height when fine
        if model.resourceShow {
            resourceMemoryStrip
        }
        if !model.attentionRows.isEmpty {
            attentionStrip
        }
        if !model.heaviestSessions.isEmpty {
            heaviestSessionsCard
        }
        if !model.toolRows.isEmpty {
            toolsCard
        }
        if !model.buzzProjects.isEmpty {
            buzzCard
        }
        if model.toolRows.isEmpty && !model.isStale && !model.isPaused {
            emptyCard
        }
    }

    /// Non-technical headroom card (self-manage + manual reclaim idle).
    private var resourceMemoryStrip: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    GlassSectionLabel(title: "Capacity", systemImage: "gauge.medium")
                    Spacer(minLength: 0)
                    Label(model.resourceStateLabel, systemImage: model.resourceIcon)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(model.resourceTint)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(model.resourceTint.opacity(0.14), in: Capsule())
                }
                if !model.resourceManagedLabel.isEmpty {
                    Text(model.resourceManagedLabel)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.green)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text(model.resourceTitle)
                    .font(.title3.weight(.bold))
                    .foregroundStyle(MonitorTheme.inkText)
                    .fixedSize(horizontal: false, vertical: true)
                capacityOverviewCard
                loadSourcesCard
                capacityActionCard
                if model.resourceAutoEnd {
                    Text("Auto-pause is on for unused background work only — never closes mid-stream work.")
                        .font(.caption2)
                        .foregroundStyle(MonitorTheme.quietText)
                }
                if !model.resourceCandidateLabel.isEmpty && model.resourceBand != "ok" {
                    HStack(spacing: 8) {
                        Image(systemName: "flame.fill")
                            .foregroundStyle(model.resourceTint)
                            .font(.caption)
                        Text(model.resourceCandidateLabel)
                            .font(.callout.weight(.semibold))
                            .foregroundStyle(model.resourceTint)
                            .lineLimit(2)
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 4)
                }
                if model.resourceBand != "ok" && !model.resourceCandidateApp.isEmpty && !model.resourceCandidateSessionId.isEmpty {
                    Button {
                        model.freeRamFromResource()
                    } label: {
                        Text(model.resourceActionLabel)
                            .font(.body.weight(.semibold))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(model.resourceTint)
                    .controlSize(.regular)
                    .accessibilityHint("Stops the suggested AI session after you confirm")
                }
            }
        }
    }

    private var capacityOverviewCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Machine")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(MonitorTheme.mutedText)
                    Text(MonitorCopy.percentLabel(model.resourceAiMemPct))
                        .font(.title2.monospacedDigit().weight(.bold))
                        .foregroundStyle(MonitorCopy.aiMemTint(model.resourceAiMemPct))
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 2) {
                    Text(capacityPlainState)
                        .font(.caption.weight(.bold))
                        .foregroundStyle(model.resourceTint)
                    Text(capacityPlainDetail)
                        .font(.caption2)
                        .foregroundStyle(MonitorTheme.mutedText)
                        .lineLimit(1)
                }
            }

            GeometryReader { geo in
                let room = MonitorCopy.roomRatio(model.resourceHeadroomMb, okMb: model.resourceHeadroomOkMb)
                let spill = MonitorCopy.backupRatio(
                    used: model.resourceSwapUsedMb,
                    total: model.resourceSwapTotalMb
                )
                let ai = min(1.0, max(0.0, (model.resourceAiMemPct ?? 0) / 100.0))
                let cpu = min(1.0, max(0.0, (model.resourceCpuCapacityPct ?? 0) / 100.0))
                let width = max(1, geo.size.width)
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(MonitorTheme.inkBlue.opacity(0.07))
                    Capsule()
                        .fill(MonitorCopy.roomTint(model.resourceHeadroomMb, okMb: model.resourceHeadroomOkMb, warnMb: model.resourceHeadroomWarnMb).opacity(0.88))
                        .frame(width: max(8, width * CGFloat(room)))
                    Capsule()
                        .fill(MonitorCopy.backupTint(
                            used: model.resourceSwapUsedMb,
                            total: model.resourceSwapTotalMb
                        ).opacity(0.35))
                        .frame(width: max(5, width * CGFloat(spill)))
                        .offset(x: width * 0.58)
                    Capsule()
                        .fill(MonitorCopy.aiMemTint(model.resourceAiMemPct).opacity(0.88))
                        .frame(width: max(5, width * CGFloat(ai)))
                        .offset(x: width * 0.08)
                    Capsule()
                        .fill(MonitorCopy.cpuTint(model.resourceCpuCapacityPct).opacity(0.65))
                        .frame(width: max(5, width * CGFloat(cpu)))
                        .offset(x: width * 0.18)
                }
                .clipShape(Capsule())
            }
            .frame(height: 12)

            VStack(spacing: 6) {
                HStack(spacing: 6) {
                    capacityMetric(title: "RAM", value: Human.mbShort(model.resourceMemsizeMb ?? -1), tint: .green)
                    capacityMetric(title: "Spill", value: storageBackupValue, tint: MonitorCopy.backupTint(used: model.resourceSwapUsedMb, total: model.resourceSwapTotalMb))
                }
                HStack(spacing: 6) {
                    capacityMetric(title: "CPU", value: MonitorCopy.percentLabel(model.resourceCpuCapacityPct), tint: MonitorCopy.cpuTint(model.resourceCpuCapacityPct))
                    capacityMetric(title: "AI", value: "\(Human.mbShort(model.resourceAiMb ?? -1)) · \(MonitorCopy.percentLabel(model.resourceAiMemPct))", tint: MonitorCopy.aiMemTint(model.resourceAiMemPct))
                }
                HStack(spacing: 6) {
                    capacityMetric(title: "Room", value: Human.mbShort(model.resourceHeadroomMb ?? -1), tint: MonitorCopy.roomTint(model.resourceHeadroomMb, okMb: model.resourceHeadroomOkMb, warnMb: model.resourceHeadroomWarnMb))
                    capacityMetric(title: "Motion", value: MonitorCopy.shufflingLabel(model.resourceThrashScore), tint: MonitorCopy.shufflingTint(model.resourceThrashScore))
                }
            }
        }
        .padding(11)
        .background(MonitorTheme.controlFill, in: .rect(cornerRadius: 15))
        .overlay {
            RoundedRectangle(cornerRadius: 15, style: .continuous)
                .stroke(MonitorTheme.darkHairline, lineWidth: 1)
        }
    }

    private var capacityPlainState: String {
        switch model.resourcePressureState {
        case "calibrating": return "Learning"
        case "ok": return "Clear"
        case "caution": return "Watch"
        case "stop_start_gate": return "Hold"
        case "freeze_risk": return "Protect"
        default: return model.resourceStateLabel
        }
    }

    private var capacityPlainDetail: String {
        if model.resourcePressureState == "calibrating" { return "Local baseline" }
        if model.resourcePressureState == "freeze_risk" { return "Save work first" }
        if !model.resourceCanStartHeavy { return "No new heavy work" }
        if model.resourcePressureState == "caution" { return "Starts allowed" }
        return "Safe to continue"
    }

    private func capacityMetric(title: String, value: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.caption2.weight(.bold))
                .foregroundStyle(MonitorTheme.quietText)
            Text(value)
                .font(.caption.monospacedDigit().weight(.bold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.68)
                .allowsTightening(true)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MonitorTheme.snow.opacity(0.72), in: .rect(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(MonitorTheme.gold.opacity(0.18), lineWidth: 1)
        }
    }

    private var resourceMonitorGrid: some View {
        VStack(spacing: 7) {
            headroomBarRow(
                title: "Room",
                value: Human.mbShort(model.resourceHeadroomMb ?? -1),
                ratio: MonitorCopy.roomRatio(model.resourceHeadroomMb, okMb: model.resourceHeadroomOkMb),
                tint: MonitorCopy.roomTint(model.resourceHeadroomMb, okMb: model.resourceHeadroomOkMb, warnMb: model.resourceHeadroomWarnMb)
            )
            headroomBarRow(
                title: "Backup",
                value: storageBackupValue,
                ratio: MonitorCopy.backupRatio(
                    used: model.resourceSwapUsedMb,
                    total: model.resourceSwapTotalMb
                ),
                tint: MonitorCopy.backupTint(
                    used: model.resourceSwapUsedMb,
                    total: model.resourceSwapTotalMb
                )
            )
            headroomDotRow(
                title: "Shuffle",
                value: MonitorCopy.shufflingLabel(model.resourceThrashScore),
                tint: MonitorCopy.shufflingTint(model.resourceThrashScore)
            )
            headroomBarRow(
                title: "AI",
                value: Human.mbShort(model.resourceAiMb ?? -1),
                ratio: MonitorCopy.aiRatio(model.resourceAiMb),
                tint: MonitorCopy.aiTint(model.resourceAiMb)
            )
        }
        .padding(.vertical, 2)
    }

    private var capacityActionCard: some View {
        let needsAction = !model.resourceCanStartHeavy || model.resourcePressureState == "freeze_risk"
        return Button {
            if needsAction {
                model.openBrowserCleanup()
            }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: needsAction ? "hand.raised.fill" : "checkmark.circle.fill")
                    .font(.body)
                    .foregroundStyle(needsAction ? model.resourceTint : .green)
                    .frame(width: 20)
                VStack(alignment: .leading, spacing: 2) {
                    Text(MonitorCopy.actionTitle(
                        state: model.resourcePressureState,
                        canStart: model.resourceCanStartHeavy
                    ))
                    .font(.caption.weight(.bold))
                    .foregroundStyle(MonitorTheme.inkText)
                    Text(MonitorCopy.actionSubtitle(
                        state: model.resourcePressureState,
                        canStart: model.resourceCanStartHeavy
                    ))
                    .font(.caption2)
                    .foregroundStyle(MonitorTheme.quietText)
                    .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                if needsAction {
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(MonitorTheme.water)
                }
            }
            .padding(9)
            .background(needsAction ? MonitorTheme.warmFill : MonitorTheme.controlFill, in: .rect(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(MonitorTheme.darkHairline, lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
        .disabled(!needsAction)
    }

    private var loadSourcesCard: some View {
        let rows = Array(model.heaviestSessions.prefix(3))
        let maxRss = max(rows.map(\.rssKb).max() ?? 1, 1)
        return Group {
            if !rows.isEmpty && model.resourceCandidateLabel.isEmpty {
                VStack(alignment: .leading, spacing: 9) {
                    HStack(spacing: 6) {
                        Text("Main load")
                            .font(.caption.weight(.bold))
                            .foregroundStyle(MonitorTheme.quietText)
                        Spacer(minLength: 0)
                        Text("\(rows.count)")
                            .font(.caption2.monospacedDigit().weight(.bold))
                            .foregroundStyle(MonitorTheme.water)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(MonitorTheme.gold.opacity(0.14), in: Capsule())
                    }
                    ForEach(Array(rows.enumerated()), id: \.element.id) { idx, row in
                        loadSourceRow(row, rank: idx + 1, maxRssKb: maxRss)
                    }
                }
                .padding(11)
                .background(MonitorTheme.controlFill, in: .rect(cornerRadius: 15))
                .overlay {
                    RoundedRectangle(cornerRadius: 15, style: .continuous)
                        .stroke(MonitorTheme.darkHairline, lineWidth: 1)
                }
            }
        }
    }

    private func loadSourceRow(_ row: SessionRow, rank: Int, maxRssKb: Int) -> some View {
        let ratio = min(1.0, max(0.06, Double(row.rssKb) / Double(max(maxRssKb, 1))))
        let tint: Color = row.state == .active ? MonitorTheme.amber : (row.state == .open ? MonitorTheme.mint : MonitorTheme.quietText)
        return HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(tint.opacity(0.14))
                Image(systemName: symbolForTool(row.app))
                    .font(.body.weight(.semibold))
                    .foregroundStyle(tint)
            }
            .frame(width: 34, height: 34)
            VStack(alignment: .leading, spacing: 3) {
                Text(row.title)
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(MonitorTheme.inkText)
                    .lineLimit(1)
                HStack(spacing: 5) {
                    Text("#\(rank)")
                        .font(.caption2.monospacedDigit().weight(.bold))
                        .foregroundStyle(MonitorTheme.quietText)
                    Text(Human.toolName(row.app))
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(MonitorTheme.quietText)
                        .lineLimit(1)
                    Text("·")
                        .font(.caption2)
                        .foregroundStyle(MonitorTheme.quietText.opacity(0.58))
                    Text(row.state.rawValue.capitalized)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(tint)
                }
            }
            Spacer(minLength: 0)
            VStack(alignment: .trailing, spacing: 5) {
                Text(row.mem)
                    .font(.caption.monospacedDigit().weight(.bold))
                    .foregroundStyle(MonitorTheme.inkText.opacity(0.92))
                GeometryReader { geo in
                    ZStack(alignment: .trailing) {
                        Capsule()
                            .fill(MonitorTheme.inkBlue.opacity(0.08))
                        Capsule()
                            .fill(tint.opacity(0.80))
                            .frame(width: max(5, geo.size.width * CGFloat(ratio)))
                    }
                }
                .frame(width: 54, height: 5)
            }
        }
        .padding(9)
        .background(MonitorTheme.snow.opacity(0.58), in: .rect(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(MonitorTheme.gold.opacity(0.16), lineWidth: 1)
        }
    }

    private var storageBackupValue: String {
        guard let used = model.resourceSwapUsedMb else { return "Unknown" }
        if let total = model.resourceSwapTotalMb, total > 0 {
            return "\(Human.mbShort(used)) / \(Human.mbShort(total))"
        }
        return Human.mbShort(used)
    }

    private func headroomBarRow(title: String, value: String, ratio: Double, tint: Color) -> some View {
        HStack(spacing: 8) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(MonitorTheme.quietText)
                .frame(width: 48, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Color.white.opacity(0.10))
                    Capsule()
                        .fill(tint.opacity(0.85))
                        .frame(width: max(4, geo.size.width * CGFloat(ratio)))
                }
            }
            .frame(height: 8)
            Text(value)
                .font(.caption.monospacedDigit().weight(.bold))
                .foregroundStyle(tint)
                .lineLimit(1)
                .frame(width: 104, alignment: .trailing)
        }
    }

    private func headroomDotRow(title: String, value: String, tint: Color) -> some View {
        HStack(spacing: 8) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(MonitorTheme.quietText)
                .frame(width: 48, alignment: .leading)
            HStack(spacing: 5) {
                Circle()
                    .fill(tint)
                    .frame(width: 7, height: 7)
                Text(value)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(tint)
            }
            Spacer(minLength: 0)
        }
    }

    private var heaviestSessionsCard: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 8) {
                // Single cue lives on row #1 badge — no prose under header or footer
                GlassSectionLabel(title: "Heaviest sessions", systemImage: "flame.fill")

                ForEach(Array(model.heaviestSessions.enumerated()), id: \.element.id) { idx, row in
                    let dynamicHeavy = model.isDynamicallyHeavy(row)
                    let badgeTitle = model.loadBadgeTitle(for: row, index: idx)
                    let rowBody = HStack(spacing: 8) {
                        Image(systemName: symbolForTool(row.app))
                            .font(.body)
                            .foregroundStyle(dynamicHeavy ? Color.red : Color.secondary)
                            .symbolRenderingMode(.hierarchical)
                            .frame(width: 20)

                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.title)
                                .font(.body.weight(.semibold))
                                .foregroundStyle(MonitorTheme.inkText)
                                .lineLimit(1)
                            Text(row.isService
                                 ? row.subtitle
                                 : "\(Human.toolName(row.app)) · \(row.state.rawValue)")
                                .font(.caption)
                                .foregroundStyle(MonitorTheme.quietText)
                                .lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .layoutPriority(0)

                        if let badgeTitle {
                            Text(badgeTitle)
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(dynamicHeavy ? .red : MonitorTheme.quietText)
                                .lineLimit(1)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3)
                                .background(Capsule().fill(dynamicHeavy ? Color.red.opacity(0.14) : Color.secondary.opacity(0.12)))
                                .layoutPriority(2)
                        }

                        MemLabel(
                            text: row.mem,
                            tint: dynamicHeavy ? .red : .primary.opacity(0.9),
                            weight: .semibold,
                            size: .callout
                        )

                        Image(systemName: row.openable ? "arrow.up.forward.app" : "slash.circle")
                            .font(.caption2)
                            .foregroundStyle(row.openable ? Color.secondary : Color.secondary.opacity(0.45))
                    }
                    .padding(.vertical, 6)
                    .padding(.horizontal, 8)
                    .contentShape(Rectangle())
                    .background {
                        if dynamicHeavy {
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(Color.red.opacity(0.10))
                        }
                    }
                    .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 10))
                    .opacity(row.openable ? 1.0 : 0.9)

                    if row.openable {
                        Button {
                            model.openSession(row)
                        } label: { rowBody }
                        .buttonStyle(.plain)
                        .accessibilityHint(idx == 0 ? "\(model.endLoadTitle) targets this session" : "Open session")
                    } else {
                        // Not a button — tap does not claim open
                        rowBody
                            .accessibilityLabel("\(row.title). \(row.openDenied)")
                            .onTapGesture {
                                model.focusNote = row.openDenied
                            }
                    }
                }
            }
        }
    }

    /// Signature Phase 2 element — zero height when empty (not rendered).
    private var attentionStrip: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 8) {
                GlassSectionLabel(title: "Needs attention", systemImage: "bell.badge.fill")
                ForEach(model.attentionRows) { row in
                    HStack(alignment: .center, spacing: 10) {
                        Image(systemName: row.symbol)
                            .foregroundStyle(row.tint)
                            .symbolRenderingMode(.hierarchical)
                            .frame(width: 22)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(Human.toolName(row.app)) · \(row.title)")
                                .font(.body.weight(.semibold))
                                .foregroundStyle(MonitorTheme.inkText)
                                .fixedSize(horizontal: false, vertical: true)
                            if !row.detail.isEmpty {
                                Text(row.detail)
                                    .font(.caption)
                                    .foregroundStyle(MonitorTheme.quietText)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Spacer(minLength: 0)
                        if row.canResume {
                            Button("Resume") { model.resumeWork(row) }
                                .buttonStyle(.glass)
                                .controlSize(.small)
                        } else if row.isNeedsYou {
                            Button("Open") {
                                model.openTool(row.app)
                            }
                            .buttonStyle(.glass)
                            .controlSize(.small)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
        }
    }

    // MARK: New session (PR-B wires real launch)

    private var newSessionContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button { model.goHome() } label: {
                Label("All tools", systemImage: "chevron.left")
                    .font(.callout.weight(.semibold))
            }
            .buttonStyle(.glass)
            .controlSize(.small)

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    GlassSectionLabel(title: "New session", systemImage: "plus.circle.fill")
                    Text(model.resourceCanStartHeavy
                        ? "Pick a tool to open a fresh chat. Folder defaults to your last project."
                         : "Start gate is closed. Opening another AI session can push this Mac into swap thrash.")
                        .font(.caption)
                        .foregroundStyle(model.resourceCanStartHeavy ? Color.secondary : Color.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            GlassCard {
                VStack(alignment: .leading, spacing: 0) {
                    let rows: [(String, String)] = {
                        if !model.catalogKnown.isEmpty {
                            return model.catalogKnown
                                .filter { $0.installed == true }
                                .map { ($0.id, $0.display) }
                        }
                        return model.toolRows.map { ($0.id, $0.title) }
                    }()
                    if rows.isEmpty {
                        Text("No installed tools detected yet.")
                            .font(.callout)
                            .foregroundStyle(MonitorTheme.quietText)
                            .padding(.vertical, 8)
                    }
                    ForEach(rows, id: \.0) { id, title in
                        Button {
                            model.launchNewSession(toolId: id)
                        } label: {
                            HStack {
                                Image(systemName: symbolForTool(id))
                                    .frame(width: 22)
                                    .foregroundStyle(.tint)
                                Text(title)
                                    .font(.body.weight(.semibold))
                                    .foregroundStyle(MonitorTheme.inkText)
                                Spacer()
                                if model.newSessionToolId == id {
                                    Image(systemName: "checkmark.circle.fill")
                                        .foregroundStyle(.green)
                                }
                                Image(systemName: "arrow.up.forward.app")
                                    .font(.caption)
                                    .foregroundStyle(model.resourceCanStartHeavy ? Color.secondary : Color.orange)
                            }
                            .padding(.vertical, 8)
                            .contentShape(Rectangle())
                        }
                        .disabled(!model.resourceCanStartHeavy)
                        .buttonStyle(.plain)
                        if id != rows.last?.0 {
                            Divider().opacity(0.35)
                        }
                    }
                }
            }
        }
    }

    // MARK: Parking Lot

    private var parkingLotContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button { model.openMore() } label: {
                Label("More", systemImage: "chevron.left")
                    .font(.callout.weight(.semibold))
            }
            .buttonStyle(.glass)
            .controlSize(.small)

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        GlassSectionLabel(title: "Parking Lot", systemImage: "parkingsign.circle")
                        Spacer(minLength: 0)
                        if model.parkingCanApply {
                            Text("\(model.parkingParkable.count) ready")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(.orange)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(Color.orange.opacity(0.14), in: Capsule())
                        } else {
                            Text("Clear")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(.green)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(Color.green.opacity(0.14), in: Capsule())
                        }
                    }
                    Text(ParkingCopy.summary(items: model.parkingParkable))
                        .font(.body.weight(.semibold))
                        .foregroundStyle(MonitorTheme.inkText)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Lowers priority. Does not close apps or active AI work.")
                        .font(.caption2)
                        .foregroundStyle(MonitorTheme.quietText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    GlassSectionLabel(title: "Can park now", systemImage: "arrow.down.to.line.compact")
                    if model.parkingParkable.isEmpty {
                        Text("Nothing safe to park right now.")
                            .font(.caption)
                            .foregroundStyle(MonitorTheme.quietText)
                    } else {
                        ForEach(model.parkingParkable) { item in
                            parkingItemRow(item, tint: .orange, fallbackAction: "Lower priority")
                        }
                    }
                    Button {
                        model.applyParkingLot()
                    } label: {
                        Text(model.parkingCanApply ? "Park \(model.parkingParkable.count) helper\(model.parkingParkable.count == 1 ? "" : "s")" : "Nothing to park")
                            .font(.body.weight(.semibold))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.orange)
                    .controlSize(.regular)
                    .disabled(!model.parkingCanApply)
                    .padding(.top, 4)
                }
            }

            if !model.parkingAlreadyParked.isEmpty {
                GlassCard {
                    VStack(alignment: .leading, spacing: 8) {
                        GlassSectionLabel(title: "Already parked", systemImage: "checkmark.circle.fill")
                        ForEach(model.parkingAlreadyParked) { item in
                            parkingItemRow(item, tint: .green, fallbackAction: "Parked")
                        }
                    }
                }
            }

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    GlassSectionLabel(title: "Will not touch", systemImage: "lock.shield.fill")
                    if model.parkingProtected.isEmpty {
                        Text("No active AI work listed in the latest sample.")
                            .font(.caption)
                            .foregroundStyle(MonitorTheme.quietText)
                    } else {
                        ForEach(model.parkingProtected.prefix(5)) { item in
                            parkingItemRow(item, tint: .green, fallbackAction: "Protected")
                        }
                    }
                }
            }
        }
    }

    private func parkingItemRow(_ item: ParkingItem, tint: Color, fallbackAction: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: tint == .orange ? "tortoise.fill" : "shield.fill")
                .font(.caption)
                .foregroundStyle(tint)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title ?? item.app ?? "Background helper")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(MonitorTheme.inkText)
                    .lineLimit(1)
                Text(item.subtitle ?? item.reason ?? fallbackAction)
                    .font(.caption2)
                    .foregroundStyle(MonitorTheme.quietText)
                    .lineLimit(2)
            }
            Spacer(minLength: 0)
            VStack(alignment: .trailing, spacing: 2) {
                Text(Human.mbShort(item.rss_mb ?? 0))
                    .font(.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(.primary.opacity(0.85))
                Text(item.action_label ?? fallbackAction)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(tint)
            }
        }
        .padding(.vertical, 4)
    }

    // MARK: App Cleanup

    private var browserCleanupContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button { model.openMore() } label: {
                Label("More", systemImage: "chevron.left")
                    .font(.callout.weight(.semibold))
            }
            .buttonStyle(.glass)
            .controlSize(.small)

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        GlassSectionLabel(title: "App Cleanup", systemImage: "rectangle.stack.badge.minus")
                        Spacer(minLength: 0)
                        Text(model.browserCandidates.isEmpty ? "Clear" : "\(model.browserCandidates.count) review")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(model.browserCandidates.isEmpty ? Color.green : Color.orange)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background((model.browserCandidates.isEmpty ? Color.green : Color.orange).opacity(0.14), in: Capsule())
                    }
                    Text(BrowserCopy.summary(items: model.browserCandidates))
                        .font(.body.weight(.semibold))
                        .foregroundStyle(MonitorTheme.inkText)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Monitor asks first. Active/frontmost apps are skipped.")
                        .font(.caption2)
                        .foregroundStyle(MonitorTheme.quietText)
                        .fixedSize(horizontal: false, vertical: true)
                    if !model.browserFrontmost.isEmpty {
                        Text("Active now: \(model.browserFrontmost)")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.green)
                    }
                }
            }

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        GlassSectionLabel(title: "Review inactive apps", systemImage: "rectangle.stack.badge.minus")
                        Spacer(minLength: 0)
                        if !model.browserCandidates.isEmpty {
                            Button("Close all") {
                                model.closeAllCleanupApps()
                            }
                            .buttonStyle(.glass)
                            .controlSize(.small)
                        }
                    }
                    if model.browserCandidates.isEmpty {
                        Text("No inactive app load above the threshold.")
                            .font(.caption)
                            .foregroundStyle(MonitorTheme.quietText)
                    } else {
                        ForEach(model.browserCandidates) { item in
                            browserItemCard(item, protected: false)
                        }
                    }
                }
            }

            if !model.browserProtected.isEmpty {
                GlassCard {
                    VStack(alignment: .leading, spacing: 8) {
                        GlassSectionLabel(title: "Will not touch", systemImage: "lock.shield.fill")
                        ForEach(model.browserProtected) { item in
                            browserItemCard(item, protected: true)
                        }
                    }
                }
            }
        }
    }

    private func browserItemCard(_ item: BrowserItem, protected: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: protected ? "shield.fill" : "globe")
                    .font(.caption)
                    .foregroundStyle(protected ? Color.green : Color.orange)
                    .frame(width: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.app ?? "App")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(MonitorTheme.inkText)
                    Text(browserMetaLine(item))
                        .font(.caption2)
                        .foregroundStyle(MonitorTheme.quietText)
                        .lineLimit(2)
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 4) {
                    Text(Human.mbShort(item.rss_mb ?? 0))
                        .font(.caption.monospacedDigit().weight(.semibold))
                        .foregroundStyle(.primary.opacity(0.85))
                    if !protected && (item.closeable ?? true) {
                        Button("Close") {
                            model.closeCleanupApp(item)
                        }
                        .buttonStyle(.glass)
                        .controlSize(.small)
                    }
                }
            }
            if let helpers = item.top_helpers, !helpers.isEmpty {
                ForEach(helpers.prefix(3)) { helper in
                    HStack(spacing: 6) {
                        Image(systemName: "puzzlepiece.extension")
                            .font(.caption2)
                            .foregroundStyle(MonitorTheme.quietText)
                            .frame(width: 18)
                        Text(helper.kind ?? helper.name ?? "helper")
                            .font(.caption2)
                            .foregroundStyle(MonitorTheme.quietText)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        Text(Human.mbShort(helper.rss_mb ?? 0))
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(MonitorTheme.quietText)
                    }
                    .padding(.leading, 26)
                }
            }
        }
        .padding(.vertical, 5)
    }

    private func browserMetaLine(_ item: BrowserItem) -> String {
        var parts: [String] = []
        if item.category == "browser" {
            if let tabs = item.tabs { parts.append("\(tabs) tabs") }
            if let windows = item.windows { parts.append("\(windows) windows") }
        }
        if let renderers = item.renderer_processes, renderers > 0 { parts.append("\(renderers) renderers") }
        if let processes = item.processes { parts.append("\(processes) processes") }
        if parts.isEmpty { parts.append(item.recommendation ?? "Review if you are done with it.") }
        return parts.joined(separator: " · ")
    }

    // MARK: More (utilities — not permanent footer clutter)

    private var moreContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button { model.goHome() } label: {
                Label("All tools", systemImage: "chevron.left")
                    .font(.callout.weight(.semibold))
            }
            .buttonStyle(.glass)
            .controlSize(.small)

            GlassCard {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        Image(systemName: model.resourceIcon)
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(model.resourceTint)
                            .symbolRenderingMode(.hierarchical)
                            .frame(width: 28, height: 28)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Monitor controls")
                                .font(.title3.weight(.bold))
                                .foregroundStyle(MonitorTheme.inkText)
                            Text(moreControlLine)
                                .font(.caption)
                                .foregroundStyle(MonitorTheme.quietText)
                                .lineLimit(2)
                        }
                        Spacer(minLength: 0)
                        Text(model.resourceStateLabel)
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(model.resourceTint)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(model.resourceTint.opacity(0.14), in: Capsule())
                    }
                    HStack(spacing: 6) {
                        moreMetricPill(
                            title: "Apps",
                            value: "\(model.browserCandidates.filter { $0.closeable ?? true }.count)",
                            tint: model.browserCandidates.isEmpty ? .secondary : .orange
                        )
                        moreMetricPill(
                            title: "Park",
                            value: "\(model.parkingParkable.count)",
                            tint: model.parkingParkable.isEmpty ? .secondary : .orange
                        )
                        moreMetricPill(
                            title: "AI",
                            value: Human.mbShort(model.resourceAiMb ?? 0),
                            tint: model.resourceTint
                        )
                    }
                }
            }

            moreSection(title: "Manage load", systemImage: "gauge.medium") {
                moreRow(
                    title: "App Cleanup",
                    subtitle: moreAppCleanupSubtitle,
                    systemImage: "rectangle.stack.badge.minus",
                    tint: .orange,
                    showsChevron: true,
                    action: model.openBrowserCleanup
                )
                moreRow(
                    title: "Parking Lot",
                    subtitle: moreParkingSubtitle,
                    systemImage: "parkingsign.circle",
                    tint: .orange,
                    showsChevron: true,
                    action: model.openParkingLot
                )
                moreRow(
                    title: model.endLoadTitle,
                    subtitle: model.endHeavyPreview.isEmpty ? "No AI target detected" : model.endHeavyPreview,
                    systemImage: "flame.fill",
                    tint: model.endLoadTitle == "End heavy" ? .red : .orange,
                    showsChevron: false,
                    action: model.endHeaviest
                )
            }

            moreSection(title: "Configure", systemImage: "switch.2") {
                moreRow(
                    title: "Shown tools",
                    subtitle: "Hide tools that do not matter here",
                    systemImage: "slider.horizontal.3",
                    tint: .blue,
                    showsChevron: true,
                    action: model.openEditTools
                )
            }

            moreSection(title: "System", systemImage: "gearshape") {
                HStack(spacing: 8) {
                    Button {
                        model.reload()
                    } label: {
                        Label("Refresh", systemImage: "arrow.clockwise")
                            .font(.caption.weight(.bold))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.glass)
                    .controlSize(.regular)

                    if model.isPaused {
                        Button {
                            model.resumeMonitoring()
                        } label: {
                            Label("Resume", systemImage: "play.fill")
                                .font(.caption.weight(.bold))
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.glass)
                        .controlSize(.regular)
                    } else {
                        Button {
                            model.pauseMonitoring()
                        } label: {
                            Label("Pause", systemImage: "pause.fill")
                                .font(.caption.weight(.bold))
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.glass)
                        .controlSize(.regular)
                    }
                }
                moreRow(
                    title: "Quit Local AI Monitor",
                    subtitle: "Close menu bar app",
                    systemImage: "xmark",
                    tint: .secondary,
                    showsChevron: false,
                    action: { NSApp.terminate(nil) }
                )
            }
        }
    }

    private var moreControlLine: String {
        if model.isPaused { return "Monitoring is paused." }
        if model.isStale { return "Monitor is not updating." }
        if !model.resourceCanStartHeavy { return "Clear load before starting more AI work." }
        return "Manage background load and Monitor settings."
    }

    private var moreAppCleanupSubtitle: String {
        let count = model.browserCandidates.filter { $0.closeable ?? true }.count
        if count == 1 { return "1 inactive app can close" }
        if count > 1 { return "\(count) inactive apps can close" }
        if !model.browserProtected.isEmpty { return "Active apps are protected" }
        return "Find inactive apps"
    }

    private var moreParkingSubtitle: String {
        let count = model.parkingParkable.count
        if count == 1 { return "1 helper can slow down" }
        if count > 1 { return "\(count) helpers can slow down" }
        if !model.parkingProtected.isEmpty { return "Active helpers are protected" }
        return "Find idle helpers"
    }

    private func moreMetricPill(title: String, value: String, tint: Color) -> some View {
        HStack(spacing: 5) {
            Text(title)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(MonitorTheme.quietText)
            Text(value)
                .font(.caption2.monospacedDigit().weight(.bold))
                .foregroundStyle(tint)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .frame(maxWidth: .infinity)
        .background(Color.white.opacity(0.045), in: Capsule())
    }

    private func moreSection<Content: View>(
        title: String,
        systemImage: String,
        @ViewBuilder content: @escaping () -> Content
    ) -> some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 8) {
                GlassSectionLabel(title: title, systemImage: systemImage)
                content()
            }
        }
    }

    private func moreRow(
        title: String,
        subtitle: String,
        systemImage: String,
        tint: Color,
        showsChevron: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 11) {
                ZStack {
                    Circle()
                        .fill(tint.opacity(0.15))
                    Image(systemName: systemImage)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(tint)
                }
                .frame(width: 28, height: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(MonitorTheme.inkText)
                    Text(subtitle)
                        .font(.caption2)
                        .foregroundStyle(MonitorTheme.quietText)
                        .lineLimit(2)
                }
                Spacer()
                if showsChevron {
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(MonitorTheme.quietText)
                }
            }
            .padding(.vertical, 7)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: Tool → sessions drill-down

    private var toolDetailContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Back
            Button {
                model.goHome()
            } label: {
                Label("All tools", systemImage: "chevron.left")
                    .font(.callout.weight(.semibold))
            }
            .buttonStyle(.glass)
            .controlSize(.small)

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        Image(systemName: symbolForTool(
                            { if case .tool(let id) = model.nav { return id } ; return "" }()
                        ))
                        .symbolRenderingMode(.hierarchical)
                        Text(model.selectedToolTitle)
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(MonitorTheme.inkText)
                        Spacer()
                    }
                    Text("Sessions — open only when a real window exists")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(MonitorTheme.quietText)

                    let toolId: String = {
                        if case .tool(let id) = model.nav { return id }
                        return ""
                    }()
                    if !toolId.isEmpty {
                        if model.isQuarantined(toolId) {
                            Button {
                                model.unquarantineTool(toolId)
                            } label: {
                                Label("Allow \(model.selectedToolTitle) again", systemImage: "lock.open.fill")
                                    .font(.caption.weight(.semibold))
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                            .tint(.green)
                        } else {
                            Button(role: .destructive) {
                                model.quarantineTool(toolId)
                            } label: {
                                Label("Quarantine — keep off", systemImage: "lock.fill")
                                    .font(.caption.weight(.semibold))
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                            .tint(.orange)
                            .help("Stops background service and blocks auto-restart")
                        }
                    }

                    let rows = model.sessionsForSelectedTool()
                    if !rows.isEmpty {
                        Button(role: .destructive) {
                            model.endAllForSelectedTool()
                        } label: {
                            Label("End all for this tool", systemImage: "xmark.circle.fill")
                                .font(.caption.weight(.semibold))
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .tint(.red)
                    }
                }
            }

            let rows = model.sessionsForSelectedTool()
            if rows.isEmpty {
                GlassCard {
                    let toolId: String = {
                        if case .tool(let id) = model.nav { return id }
                        return ""
                    }()
                    if model.isQuarantined(toolId) {
                        Text("\(model.selectedToolTitle) is quarantined — not running.")
                            .font(.body)
                            .foregroundStyle(MonitorTheme.inkText)
                    } else {
                        Text("No open sessions for this tool.")
                            .font(.body)
                            .foregroundStyle(MonitorTheme.inkText)
                    }
                }
            } else {
                GlassCard {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(rows) { row in
                            VStack(alignment: .leading, spacing: 6) {
                                HStack(spacing: 8) {
                                    if row.openable {
                                        Button {
                                            model.openSession(row)
                                        } label: {
                                            sessionRowLabel(row, showOpenArrow: true)
                                        }
                                        .buttonStyle(.plain)
                                    } else {
                                        sessionRowLabel(row, showOpenArrow: false)
                                            .opacity(0.85)
                                    }

                                    Button(role: .destructive) {
                                        model.endSession(row)
                                    } label: {
                                        Image(systemName: "xmark.circle.fill")
                                            .font(.title3)
                                            .foregroundStyle(.red)
                                            .symbolRenderingMode(.hierarchical)
                                    }
                                    .buttonStyle(.plain)
                                    .help("End session — frees RAM")
                                    .accessibilityLabel("End session")
                                }
                                // Always show what it is doing (RAM justification)
                                Text(row.activity)
                                    .font(.caption2)
                                    .foregroundStyle(MonitorTheme.quietText)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .padding(.leading, 30)
                                if !row.openable {
                                    Text(row.openDenied)
                                        .font(.caption2.weight(.medium))
                                        .foregroundStyle(.orange)
                                        .padding(.leading, 30)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func sessionRowLabel(_ row: SessionRow, showOpenArrow: Bool = true) -> some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: row.state.symbol)
                .foregroundStyle(row.state.tint)
                .symbolRenderingMode(.hierarchical)
                .frame(width: 22)

            VStack(alignment: .leading, spacing: 2) {
                Text(row.title)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(MonitorTheme.inkText)
                    .multilineTextAlignment(.leading)
                Text(row.subtitle)
                    .font(.caption)
                    .foregroundStyle(MonitorTheme.quietText)
                    .multilineTextAlignment(.leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            VStack(alignment: .trailing, spacing: 2) {
                Text(row.state.rawValue)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(row.state.tint)
                    .lineLimit(1)
                MemLabel(
                    text: row.mem,
                    tint: model.isDynamicallyHeavy(row) ? .red : .primary.opacity(0.85),
                    weight: .medium,
                    size: .caption
                )
            }

            if showOpenArrow {
                Image(systemName: "arrow.up.forward.app")
                    .font(.caption)
                    .foregroundStyle(MonitorTheme.quietText)
            } else {
                Image(systemName: "slash.circle")
                    .font(.caption)
                    .foregroundStyle(MonitorTheme.quietText.opacity(0.55))
                    .help(row.openDenied)
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 8)
        .contentShape(Rectangle())
        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 12))
    }

    // MARK: Header

    private var headerCard: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Image(systemName: statusSymbol)
                        .font(.title3)
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(statusTint)
                    GlassSectionLabel(title: "Right now", systemImage: "sparkles")
                    Spacer(minLength: 0)
                }

                Text(model.sentence)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(sentenceColor)
                    .fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.leading)

                if !model.memoryLine.isEmpty {
                    Label(model.memoryLine, systemImage: "memorychip")
                        .font(.callout)
                        .foregroundStyle(.primary.opacity(0.88))
                        .symbolRenderingMode(.hierarchical)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var statusSymbol: String {
        if model.isPaused { return "pause.circle.fill" }
        if model.isStale { return "exclamationmark.triangle.fill" }
        return "checkmark.seal.fill"
    }

    private var statusTint: Color {
        if model.isPaused || model.isStale { return .orange }
        return .green
    }

    private var sentenceColor: Color {
        if model.isPaused || model.isStale { return .orange }
        return .primary
    }

    // MARK: Tools (clickable)

    private var toolsCard: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    GlassSectionLabel(title: "By tool", systemImage: "square.grid.2x2")
                    Spacer()
                    Button {
                        model.openEditTools()
                    } label: {
                        Text("Edit")
                            .font(.caption.weight(.semibold))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(MonitorTheme.quietText)
                }
                Text("Installed AI tools on this Mac — tap for sessions")
                    .font(.caption)
                    .foregroundStyle(MonitorTheme.quietText)

                ForEach(model.toolRows) { row in
                    Button {
                        model.openTool(row.id)
                    } label: {
                        toolRow(row)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: Edit which tools appear

    private var editToolsContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {
                model.goHome()
            } label: {
                Label("All tools", systemImage: "chevron.left")
                    .font(.callout.weight(.semibold))
            }
            .buttonStyle(.glass)
            .controlSize(.small)

            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    GlassSectionLabel(title: "Which tools to show", systemImage: "slider.horizontal.3")
                    Text("Monitor finds tools installed on this Mac. Turn off any you do not want in the list.")
                        .font(.caption)
                        .foregroundStyle(MonitorTheme.quietText)
                        .fixedSize(horizontal: false, vertical: true)

                    Toggle(isOn: Binding(
                        get: { model.showIdleInstalled },
                        set: { model.setShowIdle($0) }
                    )) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Show installed when quiet")
                                .font(.body.weight(.medium))
                            Text("Off = only tools that are running right now")
                                .font(.caption2)
                                .foregroundStyle(MonitorTheme.quietText)
                        }
                    }
                    .toggleStyle(.switch)
                }
            }

            GlassCard {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(model.editToolRows) { row in
                        HStack(spacing: 10) {
                            Image(systemName: symbolForTool(row.id))
                                .frame(width: 22)
                                .foregroundStyle(.tint)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(row.title)
                                    .font(.body.weight(.semibold))
                                Text(row.installed ? "Installed" : "Not found on this Mac")
                                    .font(.caption2)
                                    .foregroundStyle(MonitorTheme.quietText)
                            }
                            Spacer()
                            Toggle(
                                "",
                                isOn: Binding(
                                    get: { row.isShown },
                                    set: { model.setToolShown(row.id, shown: $0) }
                                )
                            )
                            .labelsHidden()
                            .toggleStyle(.switch)
                        }
                        .padding(.vertical, 8)
                        if row.id != model.editToolRows.last?.id {
                            Divider().opacity(0.35)
                        }
                    }
                }
            }

            Text("Terminal: local-ai-monitor tools hide Codex")
                .font(.caption2)
                .foregroundStyle(MonitorTheme.quietText)
        }
    }

    private func toolRow(_ row: ToolRow) -> some View {
        let band = row.loadBand
        // Two-line left stack + fixed trailing mem — prevents "429 MB" wrapping to 3 lines
        return HStack(alignment: .center, spacing: 8) {
            RoundedRectangle(cornerRadius: 1.5, style: .continuous)
                .fill(band.tint.opacity(row.isQuietInstalled ? 0.35 : 0.9))
                .frame(width: 3, height: 32)

            Image(systemName: symbolForTool(row.id))
                .font(.body)
                .foregroundStyle(band.tint)
                .symbolRenderingMode(.hierarchical)
                .frame(width: 18, alignment: .center)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Text(row.title)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(row.isQuietInstalled ? Color.secondary : Color.primary)
                        .lineLimit(1)
                    if row.ownsHeaviest {
                        Image(systemName: "flame.fill")
                            .font(.caption2)
                            .foregroundStyle(.red.opacity(0.9))
                    }
                }
                HStack(spacing: 6) {
                    Text(row.detail)
                        .font(.caption)
                        .foregroundStyle(band.tint.opacity(row.isQuietInstalled ? 0.65 : 0.9))
                        .lineLimit(1)
                    if let budget = budgetChip(for: row.id) {
                        Text("·")
                            .font(.caption2)
                            .foregroundStyle(MonitorTheme.quietText.opacity(0.55))
                        Text(budget.text)
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(budget.tint)
                            .lineLimit(1)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .layoutPriority(0)

            if row.spark.contains(where: { $0 > 0 }) {
                SparklineView(values: row.spark, tint: band.tint)
                    .frame(width: 36, height: 14)
                    .layoutPriority(1)
            }

            MemLabel(
                text: row.mem,
                tint: band == .hot || row.ownsHeaviest
                    ? .red
                    : (row.isQuietInstalled ? .secondary : .primary.opacity(0.9)),
                weight: .semibold,
                size: .caption
            )

            Image(systemName: "chevron.right")
                .font(.caption.weight(.semibold))
                .foregroundStyle(MonitorTheme.quietText)
        }
        .contentShape(Rectangle())
        .padding(.vertical, 4)
        .padding(.horizontal, 2)
        .background {
            if row.ownsHeaviest {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color.red.opacity(0.07))
            }
        }
    }

    private func budgetChip(for appId: String) -> (text: String, tint: Color)? {
        guard let b = model.budgets[appId] else { return nil }
        if b.limit_state == "limited" {
            return ("Limited · resets soon", .orange)
        }
        if b.limit_state == "ready" {
            return ("Ready to resume", .green)
        }
        if let wt = b.week_tokens, wt > 0 {
            let n: String
            if wt >= 1_000_000 { n = String(format: "%.1fM", Double(wt) / 1_000_000) }
            else if wt >= 1000 { n = String(format: "%.1fK", Double(wt) / 1000) }
            else { n = "\(wt)" }
            // Honest local burn only — never invent remaining plan %
            return ("\(n) this week · local", .secondary)
        }
        return nil
    }

    private func symbolForTool(_ appId: String) -> String {
        switch appId {
        case "Grok": return "brain.head.profile"
        case "Claude CLI": return "terminal.fill"
        case "Claude Desktop": return "macwindow"
        case "Buzz": return "antenna.radiowaves.left.and.right"
        case "ChatGPT": return "text.bubble.fill"
        case "OpenClaw": return "network"
        case "Codex": return "chevron.left.forwardslash.chevron.right"
        case "OpenAI CLI": return "text.alignleft"
        case "Cursor": return "cursorarrow.and.square.on.square.dashed"
        default: return "app.fill"
        }
    }

    // MARK: Buzz — workers open session

    private var buzzCard: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 10) {
                GlassSectionLabel(title: "Buzz projects", systemImage: "person.3.fill")
                Text("Tap a worker to open it")
                    .font(.caption)
                    .foregroundStyle(MonitorTheme.quietText)

                ForEach(model.buzzProjects) { proj in
                    VStack(alignment: .leading, spacing: 6) {
                        Label(proj.title, systemImage: "folder.fill")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(MonitorTheme.inkText)
                            .symbolRenderingMode(.hierarchical)
                            .fixedSize(horizontal: false, vertical: true)

                        ForEach(proj.workers) { w in
                            Button {
                                model.openSession(w.session)
                            } label: {
                                HStack(spacing: 8) {
                                    Image(systemName: w.session.state.symbol)
                                        .foregroundStyle(w.session.state.tint)
                                    Text(w.title)
                                        .font(.callout.weight(.medium))
                                        .foregroundStyle(.primary.opacity(0.92))
                                        .fixedSize(horizontal: false, vertical: true)
                                    Spacer(minLength: 0)
                                    Text(w.session.state.rawValue)
                                        .font(.caption2.weight(.bold))
                                        .foregroundStyle(w.session.state.tint)
                                    Image(systemName: "arrow.up.forward.app")
                                        .font(.caption2)
                                        .foregroundStyle(MonitorTheme.quietText)
                                }
                                .padding(.leading, 4)
                                .padding(.vertical, 6)
                                .padding(.horizontal, 8)
                                .contentShape(Rectangle())
                                .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 10))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
    }

    private var emptyCard: some View {
        GlassCard(compact: true) {
            Label("No AI tools are busy right now.", systemImage: "moon.zzz.fill")
                .font(.callout.weight(.medium))
                .foregroundStyle(MonitorTheme.inkText)
                .symbolRenderingMode(.hierarchical)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Sparkline (12h CPU series from live.json sparks)

struct SparklineView: View {
    let values: [Double]
    let tint: Color

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let h = geo.size.height
            let pts = values
            let maxV = max(pts.max() ?? 0, 1e-9)
            Path { path in
                guard pts.count >= 2 else { return }
                for (i, v) in pts.enumerated() {
                    let x = w * CGFloat(i) / CGFloat(max(pts.count - 1, 1))
                    let y = h - (h * CGFloat(v / maxV))
                    if i == 0 {
                        path.move(to: CGPoint(x: x, y: y))
                    } else {
                        path.addLine(to: CGPoint(x: x, y: y))
                    }
                }
            }
            .stroke(tint.opacity(0.85), style: StrokeStyle(lineWidth: 1.4, lineCap: .round, lineJoin: .round))
        }
        .accessibilityHidden(true)
    }
}

// MARK: - App

@main
struct LocalAIMonitorMenuApp: App {
    @StateObject private var model = LiveModel()

    init() {
        NSApplication.shared.setActivationPolicy(.accessory)
    }

    var body: some Scene {
        MenuBarExtra {
            LocalAIMonitorPanel(model: model)
        } label: {
            // Style D: text only when fine; orange ! only for stale / paused / limited / needs you
            Group {
                if let symbol = model.chipSymbolName {
                    Label {
                        Text(model.titleText)
                            .font(.body.weight(.semibold))
                    } icon: {
                        Image(systemName: symbol)
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(.orange)
                    }
                    .labelStyle(.titleAndIcon)
                } else {
                    Text(model.titleText)
                        .font(.body.weight(.semibold))
                }
            }
            .onAppear { model.start() }
        }
        .menuBarExtraStyle(.window)
    }
}
