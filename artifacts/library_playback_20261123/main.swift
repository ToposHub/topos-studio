// Diagnostic host compiled with the unmodified topos-player production sources.
// Private telemetry is read via Mirror; no preferences are persisted here.
import AppKit
import Foundation
import Metal

func playbackDiagnosticMakeLibrary(_ device: MTLDevice, _ source: String) -> MTLLibrary? {
    do { return try device.makeLibrary(source: source, options: nil) }
    catch { fputs("Metal shader compilation: \(error)\n", stderr); return nil }
}

let cli = CommandLine.arguments
let width = Double(cli.count > 1 ? cli[1] : "1920") ?? 1920
let height = Double(cli.count > 2 ? cli[2] : "1080") ?? 1080
let filter = cli.count > 3 ? cli[3] : ""
let out = cli.count > 4 ? cli[4] : "/tmp/topos_player_comparison.json"
let media = URL(fileURLWithPath: "/Users/heng/Documents/vscode/topos-studio/tests/test_videos")
let files = try FileManager.default.contentsOfDirectory(at: media,
    includingPropertiesForKeys: nil).filter {
        ["mov", "mp4"].contains($0.pathExtension.lowercased())
        && (filter.isEmpty || $0.lastPathComponent.lowercased().contains(filter.lowercased()))
    }.sorted { $0.lastPathComponent < $1.lastPathComponent }
PreferenceKeys.registerDefaults()
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let window = NSWindow(contentRect: NSRect(x: 30, y: 30, width: width, height: height),
    styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
window.title = "Topos Player production-view diagnostic"
let view = ToposVideoPlayerView(frame: NSRect(x: 0, y: 0, width: width, height: height))
// Match PlayerWindowController.wirePlayerCallbacks in the real app.
let metalReady = view.enableMetalRendering()
if !metalReady { fputs("Metal unavailable; production app also falls back to CPU\n", stderr) }
window.contentView = view
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)

func field<T>(_ object: Any, _ name: String, as type: T.Type) -> T? {
    Mirror(reflecting: object).children.first { $0.label == name }?.value as? T
}
func core() -> MPVCore? {
    let wrapped = Mirror(reflecting: view).children.first { $0.label == "mpv" }?.value
    guard let wrapped else { return nil }
    return Mirror(reflecting: wrapped).children.first?.value as? MPVCore
}
func mean(_ values: [Double]) -> Double {
    values.isEmpty ? 0 : values.reduce(0, +) / Double(values.count)
}
func intervals(_ times: [Double]) -> [String: Double] {
    let delta = zip(times.dropFirst(), times).map { ($0 - $1) * 1000 }.sorted()
    guard !delta.isEmpty else { return [:] }
    return ["mean": mean(delta), "p95": delta[min(delta.count - 1, Int(Double(delta.count - 1) * 0.95))],
            "max": delta.last!]
}
var rows: [[String: Any]] = []
var fileIndex = 0
var started = Double.infinity
var measureStart = Double.infinity
var openMS = 0.0
var stamps: [Double] = []
var drawStamps: [Double] = []
var metalOutputSizes: [[Int]] = []
var heartbeats: [Double] = []
var lastFrame = -1
var errors: [String] = []
var baselineDrop = 0.0
var baselineDecoderDrop = 0.0
var tick: Timer?
view.onLocalPlaybackError = { errors.append($0) }

// Called only by instrumented copies of the two renderer files, immediately
// after their existing flushBuffer/commandBuffer.commit calls.
func playbackDiagnosticDidDraw(_ width: Int = 0, _ height: Int = 0) {
    let now = ProcessInfo.processInfo.systemUptime
    guard now >= measureStart else { return }
    drawStamps.append(now)
    if width > 0, !metalOutputSizes.contains([width, height]) {
        metalOutputSizes.append([width, height])
    }
}

func playbackDiagnosticCPUFrame(_ index: Int) {
    if !metalReady { playbackDiagnosticDidDraw() }
}

func nextFile() {
    guard fileIndex < files.count else {
        tick?.invalidate()
        view.unload()
        app.terminate(nil)
        return
    }
    stamps = []; drawStamps = []; metalOutputSizes = []
    heartbeats = []; errors = []; lastFrame = -1
    baselineDrop = 0; baselineDecoderDrop = 0
    started = ProcessInfo.processInfo.systemUptime
    let file = files[fileIndex]
    do {
        try view.load(path: file.path)
        view.togglePlay()
    } catch { errors.append(String(describing: error)) }
    openMS = (ProcessInfo.processInfo.systemUptime - started) * 1000
    measureStart = ProcessInfo.processInfo.systemUptime + 1.0
}

func finishFile(_ now: Double) {
    let mpv = core()
    let elapsed = now - measureStart
    var row: [String: Any] = ["file": files[fileIndex].lastPathComponent,
        "engine": mpv == nil ? (metalReady ? "topos-metal" : "topos-cpu") : "mpv-opengl",
        "window": [view.bounds.width, view.bounds.height],
        "measured_seconds": elapsed, "open_ms": openMS,
        "observed_frame_changes_fps": Double(stamps.count) / elapsed,
        "renderer_submissions_fps": Double(drawStamps.count) / elapsed,
        "renderer_interval_ms": intervals(drawStamps),
        "metal_output_sizes": metalOutputSizes,
        "observed_frame_interval_ms": intervals(stamps),
        "heartbeat_interval_ms": intervals(heartbeats), "errors": errors]
    if let mpv {
        for key in ["container-fps", "frame-drop-count", "decoder-frame-drop-count",
                    "avsync", "estimated-vf-fps", "display-fps", "mistimed-frame-count",
                    "vo-delayed-frame-count", "time-pos"] {
            if let value = mpv.getDouble(key) { row[key] = value }
        }
        for key in ["hwdec-current", "video-codec", "video-out-params/pixelformat", "mpv-version"] {
            if let value = mpv.getString(key) { row[key] = value }
        }
        row["measured_drops"] = (mpv.getDouble("frame-drop-count") ?? 0) - baselineDrop
        row["measured_decoder_drops"] = (mpv.getDouble("decoder-frame-drop-count") ?? 0) - baselineDecoderDrop
    } else if let movie = view.movie {
        let performance = movie.performanceSnapshot
        row["source_fps"] = movie.info.fps
        row["displayed_frame"] = view.displayedFrame
        row["estimated_dropped_frames"] = field(view, "estimatedDroppedFrames", as: Int.self)
        row["measured_presentation_fps"] = field(view, "measuredPresentationFPS", as: Double.self)
        row["metal_enabled"] = field(view, "metalRenderingEnabled", as: Bool.self)
        row["decoded_frames"] = performance.frameCount
        row["decode_ms_per_frame"] = performance.decodeMilliseconds / Double(max(1, performance.frameCount))
        row["upload_ms_per_frame"] = performance.metalUploadMilliseconds / Double(max(1, performance.frameCount))
    }
    rows.append(row)
    if let data = try? JSONSerialization.data(withJSONObject: rows, options: [.prettyPrinted, .sortedKeys]) {
        try? data.write(to: URL(fileURLWithPath: out))
    }
    print("\(files[fileIndex].lastPathComponent): renderer \(String(format: "%.2f", Double(drawStamps.count) / elapsed)) fps, errors=\(errors)")
    fflush(stdout)
    fileIndex += 1
    measureStart = .infinity
    view.unload()
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { nextFile() }
}
tick = Timer.scheduledTimer(withTimeInterval: 0.01, repeats: true) { _ in
    let now = ProcessInfo.processInfo.systemUptime
    guard now >= measureStart else { return }
    if heartbeats.isEmpty, let mpv = core() {
        baselineDrop = mpv.getDouble("frame-drop-count") ?? 0
        baselineDecoderDrop = mpv.getDouble("decoder-frame-drop-count") ?? 0
    }
    heartbeats.append(now)
    // Synchronous libmpv property reads can wait for its playback core. Poll
    // only the native Topos index here; mpv is measured at its flush callback.
    let frame = view.displayedFrame
    if frame != lastFrame { stamps.append(now); lastFrame = frame }
    if now - measureStart >= 3.0 { finishFile(now) }
}
DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { nextFile() }
app.run()
