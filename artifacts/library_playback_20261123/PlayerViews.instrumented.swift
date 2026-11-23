// ToposPlayer 共享播放视图（主 App 与 QuickLook 预览扩展共用）。
//
// 双引擎：
// - ToposKit 原生引擎：.topos/.toos——色彩精确（与剪辑软件逐位一致），
//   音频主时钟（AVAudioEngine），全 intra 任意帧直取；
// - libmpv 引擎：其它一切格式（MKV/AVI/WMV/FLV/网络流…，dlopen 加载，
//   缺库时提示安装）。.mov 壳 TPIC 内容 mpv 解不了时自动回退原生引擎。
//
// 播放器常规功能：空格播放/暂停、←/→ 帧步进、循环、倍速、音量/静音、
// 全屏、信息面板、控制栏自动隐藏、拖拽打开、点击画面播放、双击全屏。
// ToposImageView：.toos 静态图（滚轮缩放 + 拖拽平移 + 双击 100%/适合）。

import AppKit
import AVFoundation
import CoreGraphics
import CToposCodec
import ImageIO
import MetalKit

// MARK: - 帧缓存（解码生产 → 显示消费）

final class FrameStore {
    private let lock = NSLock()
    private var frames: [Int: CGImage] = [:]

    func get(_ index: Int) -> CGImage? {
        lock.lock(); defer { lock.unlock() }
        return frames[index]
    }

    func put(_ image: CGImage, at index: Int) {
        lock.lock(); defer { lock.unlock() }
        frames[index] = image
    }

    func clear() {
        lock.lock(); defer { lock.unlock() }
        frames.removeAll(keepingCapacity: true)
    }

    /// 只保留 around 前后窗口（auto2k 2048×1080 RGBA 一帧 ~8MB，防长片内存膨胀）。
    func prune(around: Int, window: Int = 10) {
        lock.lock(); defer { lock.unlock() }
        if frames.count > window * 2 {
            frames = frames.filter { abs($0.key - around) <= window }
        }
    }
}

// MARK: - 不抢键盘的控件（空格/方向键始终归播放器）

final class QuietSlider: NSSlider {
    override var acceptsFirstResponder: Bool { false }
}

/// 进度条：松手回调（拖拽期间连续 seek，松手恢复播放）。
final class ScrubSlider: NSSlider {
    override var acceptsFirstResponder: Bool { false }
    var onScrubEnd: (() -> Void)?

    override func mouseUp(with event: NSEvent) {
        super.mouseUp(with: event)
        onScrubEnd?()
    }
}

// MARK: - 播放解码画质偏好（偏好设置窗口/上下文菜单共用）

extension ToposVideoPlayerView {
    /// 偏好设置 → 解码档位（0=自动 ≤2K；-1=原始分辨率；1920/1280=降档）。
    static let decodeCapKey = PlaybackQualityPreference.key
    static let decodePrefChangedNotification = PlaybackQualityPreference.changedNotification

    static func currentPlaybackQuality() -> ToposMovie.DecodeQuality {
        PlaybackQualityPreference.decodeQuality
    }

    /// 暂停后的全分辨率刷新只在"自动"档做（用户显式选画质时尊重选择）。
    static func pauseRefreshQuality() -> ToposMovie.DecodeQuality? {
        PlaybackQualityPreference.pauseRefreshQuality
    }
}

// MARK: - 播放器视图

public final class ToposVideoPlayerView: NSView {

    // ---- 媒体状态（Topos 引擎） ----
    var movie: ToposMovie?
    private var audioPlayer: ToposAudioPlayer?
    private var audioNote: String?
    private var audioLoadGeneration: UInt64 = 0
    private var selectedAudioTrackIndex = 0
    var mediaGeneration: UInt64 = 0
    let decodeQueue = DispatchQueue(label: "topos.player.decode")
    private var decodeInFlight = false
    private var pendingScrubFrame: Int?
    private var fullRefreshInFlight = false
    // P1-03：解码失败追踪（帧索引 → 已重试次数；超限记入 failedFrames 跳过）
    private var decodeFailures: [Int: Int] = [:]
    private var failedFrames: Set<Int> = []
    private var decodeErrorNote: String?

    // ---- mpv 引擎（非 Topos 格式） ----
    private var mpv: MPVCore?
    private var mpvGLView: MPVGLView?
    /// mpv IPC socket 路径（偏好开启时非 nil；信息面板展示）。
    private(set) var mpvIPCPath: String?
    private var engineError: String?
    /// mpv 模式进度条以秒为单位。
    private var usesTimeSlider = false

    // ---- 播放状态（主线程） ----
    private var playing = false
    private var loopEnabled = false
    private var didNotifyMediaEnd = false
    private var muted = false
    private var volumeValue: Float = 0.9
    private var audioEqualizerGains = AudioProcessingSettings.equalizerGains
    private var rateValue: Float = 1
    private var timecodeEnabled = false
    var aspectMode: AspectMode = .follow
    private var rotationDegrees = 0
    private var flipHorizontal = false
    private var flipVertical = false
    var displayedFrame = 0
    private var presentationStatsStart = CACurrentMediaTime()
    private var presentationFrames = 0
    private var measuredPresentationFPS = 0.0
    private var estimatedDroppedFrames = 0
    private var previousPresentedFrame: Int?
    private var lastInfoRefreshAt = 0.0
    private var gpuConversionMilliseconds = 0.0
    private var gpuConversionSamples = 0
    private var adaptivePlaybackQuality: ToposMovie.DecodeQuality?
    private var adaptiveLagStartedAt: CFTimeInterval?
    private var adaptiveHealthyStartedAt: CFTimeInterval?
    private var adaptiveLastChangeAt: CFTimeInterval = 0
    private var wasPlayingBeforeScrub = false
    private var abLoopStart: Double?
    private var abLoopEnd: Double?

    // 壁钟（无音轨时的主时钟）
    private var wallStart = 0.0
    private var wallStartFrame = 0.0

    let store = FrameStore()
    var metalFrames: [Int: ToposMovie.GPUFrame] = [:]
    private var metalVideoView: ToposMetalVideoView?
    private var metalRenderingEnabled = false
    private var displayTimer: DispatchSourceTimer?

    // UI
    private let imageLayer = CALayer()
    /// P3.4：Topos 引擎字幕叠加层（libass 渲染 CGImage → contents）。
    private let subtitleOverlayLayer = CALayer()
    private var toposSubtitles: ToposSubtitleRenderer?
    // P5.1/P5.3/P5.4：色彩管理 + 审片工具状态（工具逻辑在 PlayerReviewTools.swift）
    let reviewOverlayLayer = CALayer()
    let abCompareLayer = CALayer()
    var compareSource: CompareSource?
    private var pictureInPictureLayer: AVSampleBufferDisplayLayer?
    private var pictureInPictureCaptureEnabled = false
    private let controlsBar = NSVisualEffectView()
    private let previewPanel = NSVisualEffectView()
    private let previewImageView = NSImageView()
    private let previewTimeLabel = NSTextField(labelWithString: "")
    private var previewWorkItem: DispatchWorkItem?
    private var previewGeneration: UInt64 = 0
    private let playButton = NSButton()
    private let slider = ScrubSlider()
    private let timeLabel = NSTextField(labelWithString: "")
    private let playlistButton = NSButton()
    private var loopButton = NSButton()
    private var muteButton = NSButton()
    private let volumeSlider = QuietSlider()
    private var fullscreenButton = NSButton()
    private let infoPanel = NSVisualEffectView()
    private let infoLabel = NSTextField(labelWithString: "")
    private let osdPanel = NSVisualEffectView()
    private let osdLabel = NSTextField(labelWithString: "")
    private var osdHideWorkItem: DispatchWorkItem?
    private var osdGeneration: UInt64 = 0
    private var infoVisible = false
    private var hideWorkItem: DispatchWorkItem?
    private var controlsVisible = true

    /// 拖入新文件时回调（主 App 用；QuickLook 扩展不设置则忽略）。
    public var onOpenFile: ((URL) -> Void)?
    public var onOpenFiles: (([URL]) -> Void)?
    /// Main App wires this to the owning window's playlist sidebar.
    var onTogglePlaylistSidebar: (() -> Void)? {
        didSet {
            playlistButton.isHidden = onTogglePlaylistSidebar == nil
            updateLayoutFrames()
        }
    }
    /// Main App handles panel presentation; dropped subtitle files use the same path.
    public var onLoadSubtitles: (([URL]) -> Void)?
    /// Called once when the current media reaches its end without single-item loop.
    public var onMediaEnded: (() -> Void)?
    /// Network media failures are reported separately so the app can show a localized alert.
    public var onNetworkError: ((URL, String) -> Void)?
    /// 本地媒体异步加载/解码失败（损坏文件、缺失文件、缺解码器）——主 App
    /// 弹窗告警，不再只写信息面板静默黑屏。
    public var onLocalPlaybackError: ((String) -> Void)?
    /// 媒体几何就绪（打开/参数变化）——主 App 用来适配窗口。
    public var onMediaReady: (() -> Void)?
    /// Called once after a local or mpv file has entered its ready state.
    public var onMediaLoaded: (() -> Void)?
    public var onPlaybackStateChanged: ((Bool) -> Void)?
    var onPictureInPictureFrame: ((CGImage, Double, Double) -> Void)?
    var onPictureInPictureRequested: (() -> Void)?
    var pictureInPictureAvailable = false
    var pictureInPictureActive = false
    /// 主 App 偏好设置入口；QuickLook 扩展不提供该窗口。
    var onShowPreferences: (() -> Void)?
    var onShowVideoFilters: (() -> Void)?
    var onShowAudioEffects: (() -> Void)?
    var onToggleMiniPlayer: (() -> Void)?

    static let playbackRates: [Float] = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4]
    static let contextVolumeLevels: [Float] = [0.1, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 2]

    var hasMedia: Bool { movie != nil || mpv != nil }
    var supportsTrackSelection: Bool { mpv != nil }
    var mpvAudioTracks: [MPVTrack] { mpv?.tracks.filter { $0.type == "audio" } ?? [] }
    var mpvSubtitleTracks: [MPVTrack] { mpv?.tracks.filter { $0.type == "sub" } ?? [] }
    var mpvChapters: [MPVChapter] { mpv?.chapters ?? [] }
    var toposAudioTracks: [ToposMovie.AudioTrack] { movie?.audioTracks ?? [] }
    var selectedToposAudioTrackIndex: Int { selectedAudioTrackIndex }
    var canLoadSubtitles: Bool {
        (supportsTrackSelection || (movie != nil && ToposSubtitleRenderer.isAvailable))
            && onLoadSubtitles != nil
    }
    var canShowPreferences: Bool { onShowPreferences != nil }
    var canShowVideoFilters: Bool { supportsTrackSelection && onShowVideoFilters != nil }
    var canShowAudioEffects: Bool { onShowAudioEffects != nil }
    var currentRate: Float { rateValue }
    var currentVolume: Float { volumeValue }
    var isMuted: Bool { muted }
    var isPlaying: Bool { playing }
    var isLoopEnabled: Bool { loopEnabled }
    var isTimecodeEnabled: Bool { timecodeEnabled }
    var playbackPositionSeconds: Double {
        if let mpv { return max(mpv.position, 0) }
        guard let movie, movie.info.fps > 0 else { return 0 }
        return Double(displayedFrame) / movie.info.fps
    }
    var playbackDurationSeconds: Double {
        if let mpv { return max(mpv.duration, 0) }
        guard let movie, movie.info.fps > 0 else { return 0 }
        return Double(movie.info.frameCount) / movie.info.fps
    }

    public var filePath: String?
    public var movieInfo: ToposMovie.Info? { movie?.info }

    public var videoDimensions: (Int, Int)? {
        if let m = movie { return (m.info.width, m.info.height) }
        if let mpv, mpv.width > 0, mpv.height > 0 { return (mpv.width, mpv.height) }
        return nil
    }

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        commonInit()
        observeColorPreferenceChanges()
        // 音频独占偏好开启时启动即占住默认输出设备（幂等）
        if UserDefaults.standard.bool(forKey: PreferenceKeys.audioExclusive) {
            ToposAudioOutputDevices.setHogMode(enabled: true)
        }
    }

    func installPictureInPictureLayer(_ pipLayer: AVSampleBufferDisplayLayer) {
        guard pictureInPictureLayer == nil else { return }
        pictureInPictureLayer = pipLayer
        pipLayer.isHidden = true
        pipLayer.videoGravity = .resizeAspect
        layer?.insertSublayer(pipLayer, below: imageLayer)
        pipLayer.frame = imageLayer.frame
    }

    @discardableResult
    func enableMetalRendering() -> Bool {
        guard metalVideoView == nil,
              let device = MTLCreateSystemDefaultDevice() else {
            return metalVideoView != nil
        }
        let view = ToposMetalVideoView(frame: bounds, device: device)
        guard view.isReady else { return false }
        view.onGPUConversionTime = { [weak self] milliseconds in
            guard let self else { return }
            self.gpuConversionMilliseconds += milliseconds
            self.gpuConversionSamples += 1
            if self.infoVisible { self.refreshInfoIfNeeded() }
        }
        metalVideoView = view
        metalRenderingEnabled = true
        view.isHidden = true
        addSubview(view, positioned: .below, relativeTo: controlsBar)
        view.setAspectMode(aspectMode)
        updateLayoutFrames()
        return true
    }

    func setPictureInPictureCaptureEnabled(_ enabled: Bool) {
        pictureInPictureCaptureEnabled = enabled
        pictureInPictureLayer?.isHidden = !enabled
        mpvGLView?.pictureInPictureCaptureEnabled = enabled
        if enabled { mpvGLView?.needsDisplay = true }
        if enabled, movie != nil, metalRenderingEnabled {
            pendingScrubFrame = displayedFrame
            kickFiller()
        }
    }

    private func hasCachedFrame(_ index: Int) -> Bool {
        metalFrames[index] != nil || store.get(index) != nil
    }

    /// P2-07：清空 GPU 帧缓存；movie 仍在时把纹理集归还池（等 GPU 在飞
    /// 采样结束后复用）。媒体卸载路径（movie 已置空）直接 removeAll。
    private func clearGPUFrameCache() {
        if let m = movie {
            for frame in metalFrames.values { m.recycleGPUTextures(of: frame) }
        }
        metalFrames.removeAll(keepingCapacity: true)
    }

    private func storeGPUFrame(_ frame: ToposMovie.GPUFrame) {
        metalFrames[frame.index] = frame
        // Keep a short decode runway without letting large 4K plane textures
        // accumulate while the playback clock is paused or seeking.
        let cacheLimit = 96 * 1024 * 1024
        guard metalFrames.count > 6
                || metalFrames.values.reduce(0, { $0 + $1.estimatedBytes }) > cacheLimit
        else { return }
        let ordered = metalFrames.keys.sorted { lhs, rhs in
            if lhs == frame.index { return true }
            if rhs == frame.index { return false }
            if lhs == displayedFrame { return true }
            if rhs == displayedFrame { return false }
            return abs(lhs - displayedFrame) < abs(rhs - displayedFrame)
        }
        var kept: [Int: ToposMovie.GPUFrame] = [:]
        var retainedBytes = 0
        for index in ordered {
            guard kept.count < 6, let candidate = metalFrames[index] else { break }
            if index != frame.index && retainedBytes + candidate.estimatedBytes > cacheLimit {
                continue
            }
            kept[index] = candidate
            retainedBytes += candidate.estimatedBytes
        }
        // 被逐出缓存的帧归还纹理集（纹理池复用的入口）
        for (index, candidate) in metalFrames where kept[index] == nil {
            movie?.recycleGPUTextures(of: candidate)
        }
        metalFrames = kept
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        commonInit()
    }

    private func commonInit() {
        PreferenceKeys.registerDefaults()
        wantsLayer = true
        layer = CALayer()
        layer?.backgroundColor = NSColor.black.cgColor

        imageLayer.contentsGravity = .resizeAspect
        imageLayer.backgroundColor = NSColor.black.cgColor
        layer?.addSublayer(imageLayer)
        subtitleOverlayLayer.isHidden = true
        layer?.addSublayer(subtitleOverlayLayer)
        reviewOverlayLayer.isHidden = true
        layer?.addSublayer(reviewOverlayLayer)
        abCompareLayer.isHidden = true
        layer?.addSublayer(abCompareLayer)

        buildControls()
        buildPreviewPanel()
        buildInfoPanel()
        buildOSD()

        postsFrameChangedNotifications = true
        NotificationCenter.default.addObserver(
            self, selector: #selector(layoutFrames),
            name: NSView.frameDidChangeNotification, object: self)
        // 偏好设置的解码画质变更：清缓存按新档重填（播放中即时生效）
        NotificationCenter.default.addObserver(
            self, selector: #selector(decodePrefChanged),
            name: Self.decodePrefChangedNotification, object: nil)
        NotificationCenter.default.addObserver(
            self, selector: #selector(videoPreferencesChanged(_:)),
            name: PreferenceKeys.videoChanged, object: nil)
        NotificationCenter.default.addObserver(
            self, selector: #selector(audioPreferencesChanged(_:)),
            name: PreferenceKeys.audioChanged, object: nil)

        registerForDraggedTypes([.fileURL])
    }

    /// 解码画质偏好变化：清帧缓存，当前帧按新档重取。
    @objc private func decodePrefChanged() {
        resetAdaptivePlaybackQuality()
        guard let m = movie, m.info.frameCount > 0 else { return }
        store.clear()
        clearGPUFrameCache()
        resetDecodeFailureState()
        pendingScrubFrame = displayedFrame
        kickFiller()
        if !playing, let q = Self.pauseRefreshQuality() {
            scheduleFullQualityRefresh(quality: q)
        }
    }

    @objc private func videoPreferencesChanged(_ notification: Notification) {
        guard let key = notification.userInfo?["key"] as? String else { return }
        if key == PreferenceKeys.showFrameCount {
            updateHud()
            updateLayoutFrames()
        } else if key == PreferenceKeys.controlsHideDelay, playing {
            scheduleControlsHide()
        } else if key == PreferenceKeys.osdEnabled,
                  !UserDefaults.standard.bool(forKey: PreferenceKeys.osdEnabled) {
            osdGeneration &+= 1
            osdHideWorkItem?.cancel()
            osdPanel.isHidden = true
        }
    }

    // MARK: - UI 构建

    private func symbolButton(_ symbol: String, action: Selector) -> NSButton {
        let b = NSButton()
        b.isBordered = false
        b.refusesFirstResponder = true
        b.contentTintColor = .white
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) {
            b.image = img
        }
        b.target = self
        b.action = action
        return b
    }

    private func updateAccessibilityLabels() {
        playButton.setAccessibilityLabel(L(playing ? "accessibility.pause" : "accessibility.play"))
        playButton.setAccessibilityHelp(L("accessibility.playHelp"))

        slider.setAccessibilityLabel(L("accessibility.position"))
        let position = timeLabel.stringValue.isEmpty
            ? L("accessibility.noMedia") : timeLabel.stringValue
        slider.setAccessibilityValueDescription(position)
        timeLabel.setAccessibilityLabel(L("accessibility.time"))
        timeLabel.setAccessibilityValue(position)

        playlistButton.setAccessibilityLabel(L("playlist.toggle"))
        playlistButton.setAccessibilityHelp(L("playlist.toggle"))

        loopButton.setAccessibilityLabel(L("accessibility.loop"))
        loopButton.setAccessibilityValueDescription(
            L(loopEnabled ? "accessibility.on" : "accessibility.off"))

        muteButton.setAccessibilityLabel(L(muted ? "accessibility.unmute" : "accessibility.mute"))
        muteButton.setAccessibilityValueDescription(
            L(muted ? "accessibility.muted" : "accessibility.unmuted"))

        volumeSlider.setAccessibilityLabel(L("accessibility.volume"))
        volumeSlider.setAccessibilityValueDescription(
            LF("accessibility.volumePercent", Int((muted ? 0 : volumeValue) * 100)))

        fullscreenButton.setAccessibilityLabel(L("accessibility.fullscreen"))
    }

    private func buildControls() {
        controlsBar.blendingMode = .withinWindow
        controlsBar.material = .hudWindow
        controlsBar.state = .active
        addSubview(controlsBar)

        playButton.isBordered = false
        playButton.refusesFirstResponder = true
        playButton.contentTintColor = .white
        playButton.font = NSFont.systemFont(ofSize: 18)
        playButton.title = "▶"
        playButton.target = self
        playButton.action = #selector(togglePlay)
        controlsBar.addSubview(playButton)

        slider.isContinuous = true
        slider.controlSize = .small
        slider.target = self
        slider.action = #selector(scrubChanged)
        slider.onScrubEnd = { [weak self] in self?.scrubEnded() }
        controlsBar.addSubview(slider)

        timeLabel.textColor = .white
        timeLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        controlsBar.addSubview(timeLabel)

        playlistButton.isBordered = false
        playlistButton.refusesFirstResponder = true
        playlistButton.contentTintColor = .white
        playlistButton.image = NSImage(systemSymbolName: "sidebar.right",
                                       accessibilityDescription: L("playlist.toggle"))
        playlistButton.toolTip = L("playlist.toggle")
        playlistButton.target = self
        playlistButton.action = #selector(togglePlaylistSidebar)
        playlistButton.isHidden = onTogglePlaylistSidebar == nil
        controlsBar.addSubview(playlistButton)

        loopButton = symbolButton("repeat", action: #selector(toggleLoop))
        controlsBar.addSubview(loopButton)

        muteButton = symbolButton("speaker.wave.2", action: #selector(toggleMute))
        controlsBar.addSubview(muteButton)

        volumeSlider.minValue = 0
        volumeSlider.maxValue = 2
        volumeSlider.doubleValue = Double(volumeValue)
        volumeSlider.controlSize = .small
        volumeSlider.target = self
        volumeSlider.action = #selector(volumeChanged)
        controlsBar.addSubview(volumeSlider)

        fullscreenButton = symbolButton("arrow.up.left.and.arrow.down.right",
                                        action: #selector(toggleFullscreen))
        controlsBar.addSubview(fullscreenButton)

        // VoiceOver should traverse the visible playback controls in a predictable order.
        controlsBar.setAccessibilityChildrenInNavigationOrder([
            playButton, timeLabel, slider, playlistButton, loopButton, muteButton,
            volumeSlider, fullscreenButton
        ])
        updateAccessibilityLabels()
    }

    private func buildPreviewPanel() {
        previewPanel.blendingMode = .withinWindow
        previewPanel.material = .popover
        previewPanel.state = .active
        previewPanel.isHidden = true
        previewPanel.wantsLayer = true
        previewPanel.layer?.cornerRadius = 8
        previewPanel.layer?.masksToBounds = true
        addSubview(previewPanel, positioned: .above, relativeTo: controlsBar)

        previewImageView.imageScaling = .scaleProportionallyUpOrDown
        previewImageView.imageAlignment = .alignCenter
        previewPanel.addSubview(previewImageView)

        previewTimeLabel.alignment = .center
        previewTimeLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        previewPanel.addSubview(previewTimeLabel)
        previewImageView.frame = NSRect(x: 8, y: 27, width: 212, height: 116)
        previewTimeLabel.frame = NSRect(x: 8, y: 6, width: 212, height: 16)
        previewPanel.frame.size = NSSize(width: 228, height: 149)
    }

    private func buildInfoPanel() {
        infoPanel.blendingMode = .withinWindow
        infoPanel.material = .hudWindow
        infoPanel.state = .active
        infoPanel.isHidden = true
        infoPanel.wantsLayer = true
        infoPanel.layer?.cornerRadius = 8
        addSubview(infoPanel)
        infoLabel.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        infoLabel.textColor = .white
        infoLabel.isSelectable = true
        infoPanel.addSubview(infoLabel)
    }

    private func buildOSD() {
        osdPanel.blendingMode = .withinWindow
        osdPanel.material = .hudWindow
        osdPanel.state = .active
        osdPanel.isHidden = true
        osdPanel.wantsLayer = true
        osdPanel.layer?.cornerRadius = 9
        addSubview(osdPanel)
        osdLabel.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        osdLabel.textColor = .white
        osdLabel.alignment = .center
        osdPanel.addSubview(osdLabel)
    }

    func showOSD(_ text: String) {
        guard UserDefaults.standard.bool(forKey: PreferenceKeys.osdEnabled) else { return }
        osdHideWorkItem?.cancel()
        osdLabel.stringValue = text
        osdPanel.isHidden = false
        osdPanel.alphaValue = 1
        updateLayoutFrames()
        osdGeneration &+= 1
        let generation = osdGeneration
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.25
                self.osdPanel.animator().alphaValue = 0
            }, completionHandler: {
                guard self.osdGeneration == generation else { return }
                self.osdPanel.isHidden = true
                self.osdPanel.alphaValue = 1
            })
        }
        osdHideWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9, execute: item)
    }

    // MARK: - 加载（按扩展名路由引擎）

    public func load(path: String) throws {
        try loadMedia(path: path, forceMpv: false)
    }

    public func load(url: URL) throws {
        try loadMedia(path: url.absoluteString, forceMpv: true)
    }

    private func loadMedia(path: String, forceMpv: Bool) throws {
        hideHoverPreview()
        mediaGeneration &+= 1
        resetAdaptivePlaybackQuality()
        decodeInFlight = false
        fullRefreshInFlight = false
        audioLoadGeneration &+= 1
        stopAll()
        resetVideoTransform()
        resetPresentationStats()
        detachMpv()
        metalVideoView?.isHidden = true
        movie = nil
        store.clear()
        metalFrames.removeAll(keepingCapacity: true)
        pendingScrubFrame = nil
        engineError = nil
        resetDecodeFailureState()
        didNotifyMediaEnd = false
        abLoopStart = nil
        abLoopEnd = nil
        filePath = path
        applyPlaybackDefaultsForNewMedia()
        aspectMode = AspectMode.defaultPreference

        // Topos 码流两种形态：.topos 裸容器，或 .mov 壳（stsd 4CC=TPIC，
        // 进制嗅探确认）。后者扩展名不在白名单——用统一内容分类（带缓存，
        // 与播放列表/媒体库同一结论）路由引擎，否则 TPIC 流被扔给 mpv
        // 无法解码（黑屏）。
        if !forceMpv,
           MediaContentClassifier.isToposVideo(at: URL(fileURLWithPath: path)) {
            let m = try ToposMovie(path: path)
            adoptToposMovie(m)
        } else {
            movie = nil
            try loadWithMpv(path: path)
        }
        updateInfoContent()
    }

    public func unload() {
        hideHoverPreview()
        mediaGeneration &+= 1
        resetAdaptivePlaybackQuality()
        decodeInFlight = false
        fullRefreshInFlight = false
        audioLoadGeneration &+= 1
        stopAll()
        resetVideoTransform()
        resetPresentationStats()
        detachMpv()
        MPVThumbnailProvider.shared.reset()
        toposSubtitles = nil
        subtitleOverlayLayer.isHidden = true
        movie = nil
        store.clear()
        metalFrames.removeAll(keepingCapacity: true)
        metalVideoView?.isHidden = true
        pendingScrubFrame = nil
        engineError = nil
        resetDecodeFailureState()
        filePath = nil
        didNotifyMediaEnd = false
        abLoopStart = nil
        abLoopEnd = nil
        displayedFrame = 0
        slider.doubleValue = 0
        timeLabel.stringValue = ""
        updateInfoContent()
        updateLayoutFrames()
        showControls()
    }

    /// P5.1：色彩偏好变化 → 重新评估 profile（下一帧生效）。
    private func observeColorPreferenceChanges() {
        NotificationCenter.default.addObserver(forName: PreferenceKeys.videoChanged,
                                               object: nil, queue: .main) { [weak self] _ in
            self?.colorProfileKey = nil
        }
    }

    private func applyPlaybackDefaultsForNewMedia() {
        let defaults = UserDefaults.standard
        loopEnabled = defaults.bool(forKey: PreferenceKeys.defaultLoop)
        rateValue = Float(defaults.double(forKey: PreferenceKeys.defaultRate))
        volumeValue = min(max(Float(defaults.double(forKey: PreferenceKeys.defaultVolume)), 0), 2)
        muted = defaults.bool(forKey: PreferenceKeys.rememberMute)
            ? defaults.bool(forKey: PreferenceKeys.lastMuted) : false
        // P2.4：音轨延迟默认值（会话内 [ ] 微调不写回偏好）
        audioDelaySeconds = min(max(defaults.double(forKey: PreferenceKeys.audioDelayDefault),
                                    -0.2), 0.2)
        volumeSlider.doubleValue = Double(volumeValue)
        muteButton.image = NSImage(
            systemSymbolName: muted ? "speaker.slash" : "speaker.wave.2",
            accessibilityDescription: nil)
        applyVolumeToEngines()
        updateAccessibilityLabels()
    }

    private func adoptToposMovie(_ m: ToposMovie) {
        movie = m
        selectedAudioTrackIndex = 0
        imageLayer.isHidden = false
        displayedFrame = 0
        slider.minValue = 0
        slider.maxValue = Double(max(m.info.frameCount - 1, 0))
        usesTimeSlider = false
        // 首帧走预览档（打开快）；>2K 源随后异步换全分辨率
        if metalRenderingEnabled, m.info.pixelFormat <= 1,
           let first = try? m.gpuFrame(at: 0, quality: Self.currentPlaybackQuality(),
                                       makePreviewImage: true) {
            storeGPUFrame(first)
            showMetalFrame(first)
        } else if let first = try? m.frame(at: 0, quality: Self.currentPlaybackQuality()) {
            showFrame(0, image: first.image)
        }
        updateLayoutFrames()
        setupToposSubtitles(for: m, path: filePath)
        loadAudioAsync(path: filePath ?? "", movie: m,
                       generation: audioLoadGeneration)
        if let q = Self.pauseRefreshQuality() {
            scheduleFullQualityRefresh(quality: q)
        }
        onMediaReady?()
        onMediaLoaded?()
    }

    private func loadAudioAsync(path: String, movie: ToposMovie,
                                generation: UInt64) {
        audioPlayer = nil
        audioNote = nil
        guard movie.audioTracks.indices.contains(selectedAudioTrackIndex) else {
            audioNote = L("audio.noTrack")
            return
        }
        let selectedIndex = selectedAudioTrackIndex
        let track = movie.audioTracks[selectedIndex]
        let url = URL(fileURLWithPath: path)
        decodeQueue.async { [weak self] in
            let fileCandidate = selectedIndex == 0
                ? ToposAudioDecoder.openFileSource(fileURL: url) : nil
            let streamingCandidate: ToposAudioSource?
            if let fileCandidate,
               fileCandidate.channelCount == track.channelCount,
               abs(fileCandidate.sampleRate - Double(track.sampleRate)) < 1 {
                streamingCandidate = fileCandidate
            } else {
                streamingCandidate = ToposAudioDecoder.openStreamingSource(
                    fileURL: url, trackIndex: selectedIndex,
                    sampleRate: Double(track.sampleRate),
                    channelCount: track.channelCount,
                    layoutTag: Self.channelTag(track.layout))
            }
            let source: ToposAudioSource?
            let data: ToposAudioData?
            var failureNote: String?
            if let streamingCandidate {
                source = streamingCandidate
                data = nil
            } else if track.isLPCM {
                source = nil
                if Self.canDecodeWholeTrackInMemory(track) {
                    let raw = try? movie.readAudioData(track: selectedIndex)
                    data = raw.flatMap {
                        ToposAudioDecoder.decodeLPCM(
                            $0, channels: track.channelCount,
                            bits: track.bitsPerSample, isFloat: track.isFloat32,
                            sampleRate: Double(track.sampleRate),
                            layoutTag: Self.channelTag(track.layout))
                    }
                } else {
                    data = nil
                    failureNote = L("audio.longLpcm")
                }
            } else if track.isAAC {
                source = nil
                if Self.canDecodeWholeTrackInMemory(track) {
                    data = ToposAudioDecoder.decodeCompressed(fileURL: url,
                                                              trackIndex: selectedIndex)
                } else {
                    data = nil
                    failureNote = L("audio.longAac")
                }
            } else {
                source = nil
                data = nil
                failureNote = L("audio.unsupportedCodec")
            }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.audioLoadGeneration == generation else { return }
                let preservePitch = UserDefaults.standard.bool(
                    forKey: PreferenceKeys.pitchCorrection)
                let outputDeviceSetting = UserDefaults.standard.string(
                    forKey: PreferenceKeys.audioOutputDevice) ?? "auto"
                let outputDeviceUID = outputDeviceSetting == "auto"
                    ? nil : outputDeviceSetting
                let resampleQuality = Self.resampleQualityPreference()
                let player = source.flatMap {
                    ToposAudioPlayer(source: $0, preservePitch: preservePitch,
                                     outputDeviceUID: outputDeviceUID,
                                     resampleQuality: resampleQuality)
                } ?? data.flatMap {
                    ToposAudioPlayer(data: $0, preservePitch: preservePitch,
                                     outputDeviceUID: outputDeviceUID,
                                     resampleQuality: resampleQuality)
                }
                if let p = player {
                    p.setAudioDelay(self.audioDelaySeconds)
                    p.setEqualizer(self.audioEqualizerGains)
                    p.setVolume(self.muted ? 0 : self.volumeValue)
                    p.setLoop(self.loopEnabled)
                    // 异步就绪后按「当前」播放状态接入：播放中从实时视频
                    // 位置起播（主时钟平滑交接，不回跳开头），暂停时 park
                    // 在当前帧。只看完成回调里的旧参数会让打开大文件后
                    // 立即播放/连播的音频永远接不上。
                    let currentFrame = self.playing ? self.clockFrame() : self.displayedFrame
                    let currentSeconds = Double(currentFrame) / self.fps
                    if self.playing {
                        if p.play(fromFrame: Int(currentSeconds * p.sampleRate),
                                  rate: self.rateValue) {
                            self.audioPlayer = p
                        } else {
                            // 音频设备暂不可用时，视频继续使用原有墙钟。
                            self.anchorWall(at: Double(currentFrame))
                        }
                    } else {
                        p.park(atFrame: Int(currentSeconds * p.sampleRate))
                        self.audioPlayer = p
                    }
                } else {
                    self.audioNote = failureNote ?? L(track.isAAC
                        ? "audio.aacFailed" : "audio.decodeFailed")
                }
                if self.infoVisible { self.updateInfoContent() }
            }
        }
    }

    func selectToposAudioTrack(_ index: Int) {
        guard let movie, movie.audioTracks.indices.contains(index),
              selectedAudioTrackIndex != index else { return }
        selectedAudioTrackIndex = index
        audioLoadGeneration &+= 1
        let currentSeconds = playbackPositionSeconds
        audioPlayer?.stop()
        audioPlayer = nil
        if playing { anchorWall(at: currentSeconds * fps) }
        loadAudioAsync(path: filePath ?? "", movie: movie,
                       generation: audioLoadGeneration)
    }

    @objc private func audioPreferencesChanged(_ notification: Notification) {
        guard let key = notification.userInfo?["key"] as? String else { return }
        if key == PreferenceKeys.audioExclusive {
            // CoreAudio hog mode（Topos 引擎即时生效；mpv 侧是 option，下次载入生效）
            ToposAudioOutputDevices.setHogMode(
                enabled: UserDefaults.standard.bool(forKey: PreferenceKeys.audioExclusive))
            return
        }
        if key == PreferenceKeys.audioOutputDevice, let mpv {
            let uid = UserDefaults.standard.string(forKey: PreferenceKeys.audioOutputDevice)
            mpv.setAudioDevice(uid: uid.flatMap {
                $0 == "auto" || ToposAudioOutputDevices.deviceID(for: $0) == nil ? nil : $0
            })
            return
        }
        guard key == PreferenceKeys.pitchCorrection || key == PreferenceKeys.audioOutputDevice,
              let movie else { return }
        let currentSeconds = playbackPositionSeconds
        audioLoadGeneration &+= 1
        audioPlayer?.stop()
        audioPlayer = nil
        if playing { anchorWall(at: currentSeconds * fps) }
        loadAudioAsync(path: filePath ?? "", movie: movie,
                       generation: audioLoadGeneration)
    }

    func isToposAudioTrackSelectable(_ index: Int) -> Bool {
        guard let movie else { return false }
        return movie.audioTracks.indices.contains(index)
    }

    func selectMPVTrack(type: String, id: Int64?) {
        mpv?.selectTrack(type: type, id: id)
    }

    func seekMPVChapter(_ index: Int) { mpv?.seekChapter(index) }
    func setMPVVideoAdjustment(_ key: String, value: Int) {
        mpv?.setVideoAdjustment(key, value: value)
    }
    func mpvVideoAdjustmentValue(_ key: String) -> Int {
        Int(mpv?.getDouble(key)?.rounded() ?? 0)
    }
    var mpvDeinterlaceEnabled: Bool {
        let value = mpv?.getString("deinterlace") ?? "no"
        return value == "yes" || value == "auto"
    }
    var mpvToneMapping: String { mpv?.getString("tone-mapping") ?? "auto" }
    var mpvUserVideoFilters: [MPVUserVideoFilter] { mpv?.customVideoFilters ?? [] }
    func setMPVDeinterlace(_ enabled: Bool) { mpv?.setDeinterlace(enabled) }
    func setMPVToneMapping(_ algorithm: String) { mpv?.setToneMapping(algorithm) }
    func addMPVVideoFilter(_ specification: String) -> Bool {
        mpv?.addVideoFilter(specification) ?? false
    }
    func removeMPVVideoFilter(label: String) { mpv?.removeVideoFilter(label: label) }

    private static func channelTag(_ layout: UInt32) -> UInt32 {
        switch layout {
        case UInt32(TC_AUDIO_LAYOUT_MONO): return UInt32(TC_AUDIO_CHAN_TAG_MONO)
        case UInt32(TC_AUDIO_LAYOUT_5_1): return UInt32(TC_AUDIO_CHAN_TAG_5_1_A)
        case UInt32(TC_AUDIO_LAYOUT_7_1): return UInt32(TC_AUDIO_CHAN_TAG_7_1_A)
        default: return UInt32(TC_AUDIO_CHAN_TAG_STEREO)
        }
    }

    /// Whole-track fallback is only allowed when its estimated peak working set
    /// stays bounded. Normal playback uses ToposAudioFileSource instead.
    private static func canDecodeWholeTrackInMemory(_ track: ToposMovie.AudioTrack) -> Bool {
        guard track.sampleCount > 0, track.channelCount > 0 else { return false }
        let (floatBytesPerFrame, floatFrameOverflow) = track.channelCount
            .multipliedReportingOverflow(by: MemoryLayout<Float>.size)
        guard !floatFrameOverflow else { return false }
        let (floatBytes, floatOverflow) = track.sampleCount
            .multipliedReportingOverflow(by: floatBytesPerFrame)
        guard !floatOverflow else { return false }
        if track.isAAC {
            let (peak, overflow) = floatBytes.multipliedReportingOverflow(by: 2)
            return !overflow && peak <= 256 * 1024 * 1024
        }
        let sampleBytes = track.isFloat32 ? MemoryLayout<Float>.size
            : max(track.bitsPerSample / 8, 1)
        let (bytesPerFrame, frameOverflow) = track.channelCount
            .multipliedReportingOverflow(by: sampleBytes)
        guard !frameOverflow else { return false }
        let (rawBytes, rawOverflow) = track.sampleCount
            .multipliedReportingOverflow(by: bytesPerFrame)
        guard !rawOverflow else { return false }
        let (doubleRaw, copyOverflow) = rawBytes.multipliedReportingOverflow(by: 2)
        let (peak, sumOverflow) = doubleRaw.addingReportingOverflow(floatBytes)
        return !copyOverflow && !sumOverflow && peak <= 256 * 1024 * 1024
    }

    private func loadWithMpv(path: String) throws {
        var loadError: String?
        let hwdec = UserDefaults.standard.string(forKey: PreferenceKeys.hwDecoding) ?? "auto"
        var overrides = [
            "hwdec": hwdec,
            "sub-auto": UserDefaults.standard.bool(
                forKey: PreferenceKeys.autoLoadSubtitles) ? "fuzzy" : "no",
            "sub-font": UserDefaults.standard.string(forKey: PreferenceKeys.subtitleFont)
                ?? "Helvetica",
            "sub-font-size": String(UserDefaults.standard.integer(
                forKey: PreferenceKeys.subtitleFontSize)),
            "sub-color": UserDefaults.standard.string(forKey: PreferenceKeys.subtitleColor)
                ?? "1.0/1.0/1.0",
            "sub-border-size": String(UserDefaults.standard.double(
                forKey: PreferenceKeys.subtitleBorderSize)),
        ]
        let audioDeviceUID = UserDefaults.standard.string(
            forKey: PreferenceKeys.audioOutputDevice) ?? "auto"
        overrides["audio-device"] = audioDeviceUID == "auto"
            || ToposAudioOutputDevices.deviceID(for: audioDeviceUID) == nil
            ? "auto" : "coreaudio/\(audioDeviceUID)"
        overrides["volume-max"] = "200"
        if let scheme = URL(string: path)?.scheme?.lowercased(),
           ["http", "https"].contains(scheme) {
            overrides["network-timeout"] = "15"
        }
        // 音频独占（hog mode）是 init 前 option，此处一并下发
        if UserDefaults.standard.bool(forKey: PreferenceKeys.audioExclusive) {
            overrides["audio-exclusive"] = "yes"
        }
        // mpv IPC（外部控制/streamlink 配合；每实例独立 socket，临时目录）
        if UserDefaults.standard.bool(forKey: PreferenceKeys.mpvIPCEnabled) {
            let socket = NSTemporaryDirectory()
                + "topos-player-\(UUID().uuidString).sock"
            overrides["input-ipc-server"] = socket
            mpvIPCPath = socket
        } else {
            mpvIPCPath = nil
        }
        guard let core = MPVCore(overrides: overrides, loadError: &loadError) else {
            throw ToposCodecError.unsupported(
                loadError ?? "libmpv 不可用（brew install mpv）")
        }
        mpv = core
        core.setLoopFile(loopEnabled)
        core.setSpeed(rateValue)
        core.setVolume(muted ? 0 : volumeValue)
        core.setAudioEqualizer(audioEqualizerGains)
        core.setVideoRotation(rotationDegrees)
        core.setVideoFlip(horizontal: flipHorizontal, vertical: flipVertical)
        imageLayer.isHidden = true

        guard let gl = MPVGLView(videoFrame: bounds) else {
            detachMpv()
            throw ToposCodecError.unsupported("OpenGL 像素格式初始化失败")
        }
        mpvGLView = gl
        gl.pictureInPictureCaptureEnabled = pictureInPictureCaptureEnabled
        gl.onPictureInPictureFrame = { [weak self] image, position, duration in
            self?.onPictureInPictureFrame?(image, position, duration)
        }
        gl.currentPosition = { [weak core] in core?.position ?? 0 }
        gl.duration = { [weak core] in core?.duration ?? 0 }
        addSubview(gl, positioned: .below, relativeTo: controlsBar)
        guard gl.attach(to: core) else {
            detachMpv()
            throw ToposCodecError.unsupported("mpv GL 渲染上下文创建失败")
        }
        wireMpvCallbacks(core)
        core.loadFile(path: path)
        updateLayoutFrames()
    }

    private func wireMpvCallbacks(_ core: MPVCore) {
        core.onTimeChange = { [weak self] _ in
            self?.updateHud()
            self?.refreshInfoIfNeeded()
        }
        core.onDuration = { [weak self] d in
            guard let self else { return }
            self.usesTimeSlider = true
            self.slider.minValue = 0
            self.slider.maxValue = max(d, 0.1)
            self.updateHud()
        }
        core.onParamsChanged = { [weak self, weak core] in
            guard let self, let core, self.mpv === core else { return }
            self.updateLayoutFrames()
            self.onMediaReady?()
        }
        core.onLoaded = { [weak self, weak core] in
            guard let self, let core, self.mpv === core else { return }
            self.onMediaReady?()
            self.onMediaLoaded?()
        }
        core.onEndReached = { [weak self, weak core] in
            guard let self, let core, self.mpv === core, !self.loopEnabled,
                  !self.didNotifyMediaEnd else { return }
            self.didNotifyMediaEnd = true
            self.playing = false
            self.playButton.title = "▶"
            self.updateAccessibilityLabels()
            self.showControls()
            self.onPlaybackStateChanged?(false)
            self.markWatchedOnEnd()
            self.onMediaEnded?()
        }
        core.onPauseChanged = { [weak self, weak core] paused in
            guard let self, let core, self.mpv === core else { return }
            self.playing = !paused
            self.playButton.title = paused ? "▶" : "⏸"
            self.updateAccessibilityLabels()
            self.onPlaybackStateChanged?(!paused)
        }
        core.onFileError = { [weak self, weak core] msg in
            guard let self, let core, self.mpv === core else { return }
            // .mov 壳 TPIC 内容：mpv 解不了 → 自动回退 Topos 原生引擎
            if let p = self.filePath,
               ToposSniff.isToposMovie(path: p) {
                self.detachMpv()
                if let m = try? ToposMovie(path: p) {
                    self.adoptToposMovie(m)
                    return
                }
            }
            self.engineError = msg
            // 播放状态复位：加载已失败，UI 不停在"播放中"的黑屏上
            self.playing = false
            self.displayTimer?.cancel()
            self.displayTimer = nil
            self.playButton.title = "▶"
            self.updateAccessibilityLabels()
            self.updateInfoContent()
            if self.infoVisible { self.updateLayoutFrames() }
            self.showControls()
            self.onPlaybackStateChanged?(false)
            let path = self.filePath ?? ""
            let scheme = URL(string: path)?.scheme?.lowercased()
                ?? URL(fileURLWithPath: path).scheme?.lowercased()
            if ["http", "https", "rtmp", "rtsp"].contains(scheme ?? "") {
                self.onNetworkError?(URL(string: path)!, msg)
            } else {
                // P1-04：本地文件失败同样要有默认可见告警，播放列表可继续
                self.onLocalPlaybackError?(msg)
            }
        }
    }

    private func detachMpv() {
        mpvGLView?.detach()
        mpvGLView?.removeFromSuperview()
        mpvGLView = nil
        mpv?.destroy()
        mpv = nil
        usesTimeSlider = false
        imageLayer.isHidden = false
    }

    // MARK: - 播放控制（双引擎分派）

    private var frameCount: Int { movie?.info.frameCount ?? 0 }
    private var fps: Double {
        let value = movie?.info.fps ?? mpv?.fps ?? 24
        return value.isFinite && value > 0 ? value : 24
    }
    private var isLoaded: Bool { movie != nil || mpv != nil }

    @objc public func togglePlay() {
        playing ? pausePlayback() : startPlayback()
    }

    private func startPlayback() {
        if PlaybackQualityPreference.current == .automatic,
           adaptivePlaybackQuality != nil {
            resetAdaptivePlaybackQuality()
            store.clear()
            clearGPUFrameCache()
            pendingScrubFrame = displayedFrame
        }
        presentationStatsStart = CACurrentMediaTime()
        presentationFrames = 0
        previousPresentedFrame = movie == nil ? nil : displayedFrame
        if let mpv {
            if mpv.eofReached {
                didNotifyMediaEnd = false
                mpv.seekAbsolute(0)
            }
            mpv.setPaused(false)
            playing = true
            playButton.title = "⏸"
            updateAccessibilityLabels()
            onPlaybackStateChanged?(true)
            scheduleControlsHide()
            return
        }
        guard let m = movie, m.info.frameCount > 0 else { return }
        if !loopEnabled, displayedFrame >= m.info.frameCount - 1 {
            didNotifyMediaEnd = false
            displayedFrame = 0  // 播完后再按播放 → 从头
        }
        playing = true
        playButton.title = "⏸"
        updateAccessibilityLabels()
        onPlaybackStateChanged?(true)
        if let ap = audioPlayer {
            ap.setLoop(loopEnabled)
            if !ap.play(fromFrame: frameAtAudio(displayedFrame), rate: rateValue) {
                audioPlayer = nil
                anchorWall(at: Double(displayedFrame))
            }
        } else {
            anchorWall(at: Double(displayedFrame))
        }
        startDisplayTimer()
        kickFiller()
        scheduleControlsHide()
    }

    private func pausePlayback() {
        if mpv != nil {
            mpv?.setPaused(true)
            playing = false
            playButton.title = "▶"
            updateAccessibilityLabels()
            showControls()
            onPlaybackStateChanged?(false)
            return
        }
        guard let m = movie, m.info.frameCount > 0 else { return }
        let wasAdaptivelyDownshifted = adaptivePlaybackQuality != nil
        playing = false
        resetAdaptivePlaybackQuality()
        if wasAdaptivelyDownshifted {
            store.clear()
            clearGPUFrameCache()
            pendingScrubFrame = nil
        }
        playButton.title = "▶"
        updateAccessibilityLabels()
        onPlaybackStateChanged?(false)
        displayTimer?.cancel()
        displayTimer = nil
        if let ap = audioPlayer {
            ap.pause()
        } else {
            wallStartFrame = clockMonotonicFrames()
        }
        // 宿主时间线同款：暂停后异步换全分辨率帧（>2K 源可见提升；
        // 用户显式选画质档时跳过——尊重偏好）
        if let q = Self.pauseRefreshQuality() {
            scheduleFullQualityRefresh(quality: q)
        }
        showControls()
    }

    private func stopAll() {
        let wasPlaying = playing
        playing = false
        displayTimer?.cancel()
        displayTimer = nil
        audioPlayer?.stop()
        audioPlayer = nil
        mpv?.setPaused(true)
        playButton.title = "▶"
        updateAccessibilityLabels()
        if wasPlaying { onPlaybackStateChanged?(false) }
    }

    /// 视频帧 → 音频数据域帧。
    private func frameAtAudio(_ frame: Int) -> Int {
        guard let ap = audioPlayer else { return frame }
        return Int((Double(frame) / fps) * ap.sampleRate)
    }

    private func anchorWall(at frame: Double) {
        wallStart = CACurrentMediaTime()
        wallStartFrame = frame
    }

    /// 主时钟（单调帧，循环时由调用方取模）。
    private func clockMonotonicFrames() -> Double {
        if let ap = audioPlayer {
            return ap.positionSeconds() * fps
        }
        return wallStartFrame
            + (CACurrentMediaTime() - wallStart) * fps * Double(rateValue)
    }

    private func clockFrame() -> Int {
        let mf = clockMonotonicFrames()
        let c = frameCount
        guard c > 0 else { return 0 }
        let f = Int(max(mf, 0))
        return loopEnabled ? f % c : min(f, c - 1)
    }

    private func startDisplayTimer() {
        displayTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 1.0 / 60.0, repeating: 1.0 / 60.0)
        timer.setEventHandler { [weak self] in self?.displayTick() }
        timer.resume()
        displayTimer = timer
    }

    private func displayTick() {
        guard playing, let m = movie, m.info.frameCount > 0 else { return }
            if let start = abLoopStart, let end = abLoopEnd,
           playbackPositionSeconds >= end {
            seekSeconds(start)
            return
        }
        let target = clockFrame()
        updateAdaptivePlaybackQuality(targetFrame: target, movie: m)

        if let img = store.get(target) {
            if target != displayedFrame {
                showFrame(target, image: img)
            }
        } else if let frame = metalFrames[target], target != displayedFrame {
            showMetalFrame(frame)
        }
        refreshInfoIfNeeded()

        // 到达终点（非循环）
        if !loopEnabled, target >= m.info.frameCount - 1,
           displayedFrame >= m.info.frameCount - 1, !didNotifyMediaEnd {
            didNotifyMediaEnd = true
            pausePlayback()
            markWatchedOnEnd()
            onMediaEnded?()
        }
        kickFiller()
    }

    // MARK: 解码填充（Topos 引擎；主线程调度 + 串行队列执行）
    // 播放/拖动走 auto2k 降档解码（宿主时间线同款），暂停后异步换全分辨率。

    /// P4.5：播完自动标已看（仅本地文件；未入库条目 no-op）。
    private func markWatchedOnEnd() {
        guard let path = filePath,
              URL(fileURLWithPath: path).scheme == nil || path.hasPrefix("/") else { return }
        if let store = try? MediaLibraryScanner.shared.store() {
            do { try store.setWatched(true, for: path) }
            catch { DiagnosticsLogger.shared.log(.ui, "已看标记写入失败：\(error)") }
        }
    }

    /// P1-03：同一帧重试上限（损坏帧不再无限占用解码队列）。
    private static let decodeRetryLimit = 3
    /// P1-03：坏帧缓存上限（超过则视为素材不可播，停止播放防忙循环）。
    private static let decodeFailureStopThreshold = 32

    private func kickFiller() {
        guard let m = movie, !decodeInFlight, m.info.frameCount > 0 else { return }
        let count = m.info.frameCount

        var target: Int?
        if let scrub = pendingScrubFrame {
            pendingScrubFrame = nil
            target = min(max(scrub, 0), count - 1)
        } else if playing {
            // P0-02：以主时钟目标为中心调度。旧实现固定从
            // displayedFrame+1 起补 10 帧预取——解码慢于实时时时钟目标
            // 越过窗口后永远轮不到解码，displayedFrame 停滞，播放永久冻结。
            let clockTarget = clockFrame()
            if !hasCachedFrame(clockTarget), !failedFrames.contains(clockTarget) {
                // 时钟目标缺失：直取目标帧（跳帧追上时钟）
                target = clockTarget
            } else if !hasCachedFrame(displayedFrame),
                      !failedFrames.contains(displayedFrame) {
                target = displayedFrame
            } else {
                // 短预取窗口：从时钟目标向后补 6 帧
                let lookahead = min(clockTarget + 6, count - 1)
                var f = clockTarget + 1
                while f <= lookahead {
                    if !hasCachedFrame(f), !failedFrames.contains(f) {
                        target = f
                        break
                    }
                    f += 1
                }
            }
        }
        guard let index = target else { return }
        let quality = adaptivePlaybackQuality ?? Self.currentPlaybackQuality()
        let generation = mediaGeneration
        let useMetal = metalRenderingEnabled && m.info.pixelFormat <= 1
        let capturePreview = pictureInPictureCaptureEnabled
        decodeInFlight = true
        decodeQueue.async { [weak self] in
            guard let self else { return }
            var gpuFrame: ToposMovie.GPUFrame?
            var frame: ToposMovie.Frame?
            var decodeError: Error?
            if useMetal {
                do {
                    gpuFrame = try m.gpuFrame(at: index, quality: quality,
                                              makePreviewImage: capturePreview)
                } catch { decodeError = error }
            }
            if gpuFrame == nil {
                do {
                    frame = try m.frame(at: index, quality: quality)
                } catch {
                    decodeError = decodeError ?? error
                }
            }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.mediaGeneration == generation else { return }
                self.decodeInFlight = false
                guard quality == (self.adaptivePlaybackQuality
                                  ?? Self.currentPlaybackQuality()) else {
                    if self.playing || self.pendingScrubFrame != nil {
                        self.kickFiller()
                    }
                    return
                }
                if let gpuFrame {
                    self.clearDecodeFailure(at: index)
                    self.storeGPUFrame(gpuFrame)
                    if !self.playing {
                        self.showMetalFrame(gpuFrame)
                    }
                } else if let frame {
                    self.clearDecodeFailure(at: index)
                    self.store.put(frame.image, at: index)
                    if !self.playing {
                        self.showFrame(index, image: frame.image)
                    }
                } else {
                    // P1-03：解码失败不再被 try? 吞掉——限额重试、记录原始
                    // 错误并给出可读提示；超限跳帧，坏帧过多则停止播放。
                    self.recordDecodeFailure(at: index, error: decodeError)
                }
                if self.playing || self.pendingScrubFrame != nil {
                    self.kickFiller()
                }
            }
        }
    }

    private func recordDecodeFailure(at index: Int, error: Error?) {
        let attempts = (decodeFailures[index] ?? 0) + 1
        if attempts < Self.decodeRetryLimit {
            decodeFailures[index] = attempts
            return
        }
        decodeFailures[index] = nil
        failedFrames.insert(index)
        let message = error.map { "\($0)" } ?? L("alert.decodeUnknown")
        if decodeErrorNote == nil {
            decodeErrorNote = LF("alert.frameDecodeFailed", index + 1, message)
            showOSD(decodeErrorNote!)
        }
        if infoVisible { updateInfoContent() }
        if failedFrames.count >= Self.decodeFailureStopThreshold {
            decodeErrorNote = LF("alert.decodeAborted", message)
            showOSD(decodeErrorNote!)
            if infoVisible { updateInfoContent() }
            pausePlayback()
        }
    }

    private func clearDecodeFailure(at index: Int) {
        guard decodeFailures[index] != nil else { return }
        decodeFailures[index] = nil
    }

    private func resetDecodeFailureState() {
        decodeFailures.removeAll(keepingCapacity: true)
        failedFrames.removeAll(keepingCapacity: true)
        decodeErrorNote = nil
    }

    private func updateAdaptivePlaybackQuality(targetFrame: Int, movie: ToposMovie) {
        guard PlaybackQualityPreference.current == .automatic,
              max(movie.info.width, movie.info.height) > 1280 else {
            if adaptivePlaybackQuality != nil {
                resetAdaptivePlaybackQuality()
            }
            adaptiveLagStartedAt = nil
            adaptiveHealthyStartedAt = nil
            return
        }

        let now = CACurrentMediaTime()
        let lag: Int
        if targetFrame >= displayedFrame {
            lag = targetFrame - displayedFrame
        } else if loopEnabled {
            lag = targetFrame + movie.info.frameCount - displayedFrame
        } else {
            lag = 0
        }
        let downshiftThreshold = max(4, Int((fps * 0.15).rounded(.up)))
        let recoveryThreshold = max(1, Int((fps * 0.03).rounded(.up)))

        if adaptivePlaybackQuality == nil {
            if lag >= downshiftThreshold {
                if adaptiveLagStartedAt == nil { adaptiveLagStartedAt = now }
                if now - (adaptiveLagStartedAt ?? now) >= 0.75 {
                    setAdaptivePlaybackQuality(.capped(1280), message: "osd.qualityDownshift")
                }
            } else {
                adaptiveLagStartedAt = nil
            }
            return
        }

        adaptiveLagStartedAt = nil
        if lag <= recoveryThreshold {
            if adaptiveHealthyStartedAt == nil { adaptiveHealthyStartedAt = now }
            if now - adaptiveLastChangeAt >= 30,
               now - (adaptiveHealthyStartedAt ?? now) >= 30 {
                setAdaptivePlaybackQuality(nil, message: "osd.qualityRecovered")
            }
        } else {
            adaptiveHealthyStartedAt = nil
        }
    }

    private func setAdaptivePlaybackQuality(_ quality: ToposMovie.DecodeQuality?, message: String) {
        guard adaptivePlaybackQuality != quality else { return }
        adaptivePlaybackQuality = quality
        adaptiveLastChangeAt = CACurrentMediaTime()
        adaptiveLagStartedAt = nil
        adaptiveHealthyStartedAt = nil
        store.clear()
        clearGPUFrameCache()
        resetDecodeFailureState()
        pendingScrubFrame = displayedFrame
        showOSD(L(message))
        kickFiller()
    }

    private func resetAdaptivePlaybackQuality() {
        adaptivePlaybackQuality = nil
        adaptiveLagStartedAt = nil
        adaptiveHealthyStartedAt = nil
        adaptiveLastChangeAt = 0
    }

    /// 暂停/停止拖动后：当前帧异步换高画质（仅"自动"档时升到全分辨率；
    /// 播放中途或帧已变化则丢弃，防闪烁）。P2.2：等待期间 OSD 提示，
    /// 完成后确认——大图（6K/8K V7-B）刷新 1s+ 时用户可感知进度。
    private func scheduleFullQualityRefresh(quality: ToposMovie.DecodeQuality) {
        guard let m = movie, !fullRefreshInFlight,
              m.info.frameCount > 0, !playing else { return }
        let target = displayedFrame
        let generation = mediaGeneration
        let useMetal = metalRenderingEnabled && m.info.pixelFormat <= 1
        let capturePreview = pictureInPictureCaptureEnabled
        fullRefreshInFlight = true
        if UserDefaults.standard.bool(forKey: PreferenceKeys.showFullRefreshStatus) {
            showOSD(L("osd.fullRefreshStarted"))
        }
        decodeQueue.async { [weak self] in
            guard let self else { return }
            let gpuFrame = useMetal
                ? try? m.gpuFrame(at: target, quality: quality,
                                  makePreviewImage: capturePreview)
                : nil
            let frame = gpuFrame == nil ? try? m.frame(at: target, quality: quality) : nil
            DispatchQueue.main.async { [weak self] in
                guard let self, self.mediaGeneration == generation else { return }
                self.fullRefreshInFlight = false
                if let gpuFrame, !self.playing, self.displayedFrame == target {
                    self.storeGPUFrame(gpuFrame)
                    self.showMetalFrame(gpuFrame)
                    if UserDefaults.standard.bool(forKey: PreferenceKeys.showFullRefreshStatus) {
                        self.showOSD(L("osd.fullRefreshDone"))
                    }
                } else if let frame, !self.playing, self.displayedFrame == target {
                    self.store.put(frame.image, at: target)
                    self.showFrame(target, image: frame.image)
                    if UserDefaults.standard.bool(forKey: PreferenceKeys.showFullRefreshStatus) {
                        self.showOSD(L("osd.fullRefreshDone"))
                    }
                }
            }
        }
    }

    // MARK: - 帧显示 / HUD

    private func showFrame(_ index: Int, image: CGImage) {
        displayedFrame = index
        metalVideoView?.isHidden = true
        imageLayer.isHidden = false
        let displayImage = transformedImage(image)
        imageLayer.contents = displayImage
        store.prune(around: index)
        recordPresentation(index)
        updateHud()
        refreshToposSubtitleOverlay()
        updateReviewOverlay()
        refreshABCompare()
        ScopePanelController.shared.feed(image: image)
        onPictureInPictureFrame?(displayImage, playbackPositionSeconds,
                                 playbackDurationSeconds)
    }

    private func showMetalFrame(_ frame: ToposMovie.GPUFrame) {
        guard let metalVideoView else { return }
        applyDisplayColorManagement(for: frame)
        displayedFrame = frame.index
        imageLayer.isHidden = true
        metalVideoView.isHidden = false
        metalVideoView.display(frame, aspectMode: aspectMode,
                               rotationDegrees: rotationDegrees,
                               flipHorizontal: flipHorizontal,
                               flipVertical: flipVertical)
        recordPresentation(frame.index)
        updateHud()
        refreshToposSubtitleOverlay()
        updateReviewOverlay()
        refreshABCompare()
        ScopePanelController.shared.feed(image: frame.image)
        if let image = frame.image {
            onPictureInPictureFrame?(transformedImage(image), playbackPositionSeconds,
                                     playbackDurationSeconds)
        }
    }

    /// P3.4：当前帧时间渲染 libass 字幕到叠加层（无事件则隐藏）。
    private func refreshToposSubtitleOverlay() {
        guard let toposSubtitles, toposSubtitles.isLoaded, let m = movie else {
            subtitleOverlayLayer.isHidden = true
            return
        }
        let time = Double(displayedFrame) / max(m.info.fps, 1)
        let boundsSize = bounds.size
        guard boundsSize.width > 2, boundsSize.height > 2 else { return }
        if let image = toposSubtitles.renderImage(at: time) {
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            subtitleOverlayLayer.contents = image
            subtitleOverlayLayer.contentsGravity = .resize
            subtitleOverlayLayer.frame = bounds
            subtitleOverlayLayer.isHidden = false
            CATransaction.commit()
        } else {
            subtitleOverlayLayer.isHidden = true
        }
    }

    /// P3.4：为 Topos 媒体装配字幕渲染器并尝试同名字幕。
    private func setupToposSubtitles(for movie: ToposMovie, path: String?) {
        toposSubtitles = nil
        subtitleOverlayLayer.isHidden = true
        guard ToposSubtitleRenderer.isAvailable, movie.info.width > 0 else { return }
        let defaults = UserDefaults.standard
        let colorText = defaults.string(forKey: PreferenceKeys.subtitleColor) ?? "1.0/1.0/1.0"
        let rgb = colorText.split(separator: "/").compactMap { Double($0) }
        guard let renderer = ToposSubtitleRenderer(
            preferredFont: defaults.string(forKey: PreferenceKeys.subtitleFont) ?? "PingFang SC",
            fontSize: defaults.integer(forKey: PreferenceKeys.subtitleFontSize),
            colorRGB: (rgb.count == 3 ? (rgb[0], rgb[1], rgb[2]) : (1, 1, 1)),
            borderSize: defaults.double(forKey: PreferenceKeys.subtitleBorderSize),
            videoWidth: movie.info.width,
            videoHeight: movie.info.height) else { return }
        toposSubtitles = renderer
        if let path {
            let base = URL(fileURLWithPath: path).deletingPathExtension()
            for ext in ["srt", "ass", "ssa"] {
                let candidate = base.appendingPathExtension(ext).path
                if FileManager.default.fileExists(atPath: candidate),
                   renderer.load(file: candidate) {
                    break
                }
            }
        }
    }

    /// P5.1：显示色彩管理（开关/目标 profile/源矩阵三元缓存）。
    private var colorProfileKey: String?

    func applyDisplayColorManagement(for frame: ToposMovie.GPUFrame) {
        guard let metalVideoView else { return }
        let enabled = UserDefaults.standard.bool(forKey: PreferenceKeys.colorManagementEnabled)
        let mode = UserDefaults.standard.string(forKey: PreferenceKeys.displayProfileMode) ?? "auto"
        let key = enabled ? "\(mode)-\(frame.colorMatrix)" : "off"
        guard key != colorProfileKey else { return }
        colorProfileKey = key
        guard enabled else {
            metalVideoView.applyColorProfile(nil, colorSpace: nil, extendedRange: false)
            return
        }
        var profile = DisplayColorProfile.identity
        var colorSpace: NSColorSpace?
        switch mode {
        case "srgb":
            colorSpace = .sRGB
            profile = .make(displayPrimariesNamed: "srgb", colorMatrix: frame.colorMatrix)
        case "displayp3":
            colorSpace = .displayP3
            profile = .make(displayPrimariesNamed: "displayp3", colorMatrix: frame.colorMatrix)
        case "custom":
            if let path = UserDefaults.standard.string(forKey: "pref.displayCustomICCPath"),
               let data = FileManager.default.contents(atPath: path) {
                colorSpace = NSColorSpace(iccProfileData: data)
                profile = .make(iccData: data, colorMatrix: frame.colorMatrix)
            }
        default:
            // 自动：跟随当前屏的 colorspace（含其 ICC）
            colorSpace = window?.screen?.colorSpace ?? .sRGB
            if let data = colorSpace?.iccProfileData {
                profile = .make(iccData: data, colorMatrix: frame.colorMatrix)
            } else {
                profile = .make(displayPrimariesNamed: "srgb", colorMatrix: frame.colorMatrix)
            }
        }
        metalVideoView.applyColorProfile(profile, colorSpace: colorSpace,
                                         extendedRange: false)
    }

    private func recordPresentation(_ index: Int) {
        guard playing else { return }
        guard previousPresentedFrame != index else { return }
        if let previousPresentedFrame, index > previousPresentedFrame + 1 {
            estimatedDroppedFrames += index - previousPresentedFrame - 1
        }
        previousPresentedFrame = index
        presentationFrames += 1
        playbackDiagnosticCPUFrame(index)
    }

    private func resetPresentationStats() {
        presentationStatsStart = CACurrentMediaTime()
        presentationFrames = 0
        measuredPresentationFPS = 0
        estimatedDroppedFrames = 0
        previousPresentedFrame = nil
        lastInfoRefreshAt = 0
        gpuConversionMilliseconds = 0
        gpuConversionSamples = 0
    }

    private func refreshInfoIfNeeded() {
        guard infoVisible else { return }
        let now = CACurrentMediaTime()
        guard now - lastInfoRefreshAt >= 1 else { return }
        if now > presentationStatsStart {
            measuredPresentationFPS = Double(presentationFrames)
                / (now - presentationStatsStart)
        }
        presentationFrames = 0
        presentationStatsStart = now
        lastInfoRefreshAt = now
        updateInfoContent()
    }

    private func transformedImage(_ image: CGImage) -> CGImage {
        guard rotationDegrees != 0 || flipHorizontal || flipVertical else { return image }
        let swapsDimensions = rotationDegrees % 180 != 0
        let width = swapsDimensions ? image.height : image.width
        let height = swapsDimensions ? image.width : image.height
        guard let context = CGContext(data: nil, width: width, height: height,
                                      bitsPerComponent: 8, bytesPerRow: 0,
                                      space: image.colorSpace ?? CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return image }
        context.translateBy(x: CGFloat(width) / 2, y: CGFloat(height) / 2)
        context.rotate(by: -CGFloat(rotationDegrees) * .pi / 180)
        context.scaleBy(x: flipHorizontal ? -1 : 1,
                        y: flipVertical ? -1 : 1)
        context.draw(image, in: CGRect(x: -CGFloat(image.width) / 2,
                                       y: -CGFloat(image.height) / 2,
                                       width: CGFloat(image.width),
                                       height: CGFloat(image.height)))
        return context.makeImage() ?? image
    }

    private func updateHud() {
        if let m = mpv {
            slider.doubleValue = m.position
            timeLabel.stringValue = "\(formatTime(m.position)) / \(formatTime(m.duration))"
            updateAccessibilityLabels()
            return
        }
        guard let m = movie else {
            timeLabel.stringValue = ""
            updateAccessibilityLabels()
            return
        }
        slider.doubleValue = Double(displayedFrame)
        let cur = Double(displayedFrame) / m.info.fps
        let total = Double(m.info.frameCount) / m.info.fps
        timeLabel.stringValue = "\(formatTime(cur)) / \(formatTime(total))"
        if UserDefaults.standard.bool(forKey: PreferenceKeys.showFrameCount) {
            timeLabel.stringValue += "  " + LF("hud.frames", displayedFrame + 1,
                                               m.info.frameCount)
        }
        updateAccessibilityLabels()
    }

    private func formatTime(_ seconds: Double) -> String {
        if timecodeEnabled {
            let frameRate = max(fps, 1)
            let nominalFrameRate = max(Int(frameRate.rounded()), 1)
            let frame = max(Int(max(seconds, 0) * frameRate), 0)
            let totalSeconds = frame / nominalFrameRate
            return String(format: "%02d:%02d:%02d:%02d",
                          totalSeconds / 3600, (totalSeconds / 60) % 60,
                          totalSeconds % 60, frame % nominalFrameRate)
        }
        let s = Int(seconds.rounded(.down))
        if s >= 3600 {
            return String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
        }
        return String(format: "%d:%02d.%02d", s / 60, s % 60,
                      Int((seconds - Double(s)) * 100))
    }

    // MARK: - 逐帧/跳转

    public func stepFrame(_ delta: Int) {
        if let mpv {
            mpv.frameStep(delta)
            return
        }
        guard let m = movie, m.info.frameCount > 0 else { return }
        if playing { pausePlayback() }
        let target = min(max(displayedFrame + delta, 0), m.info.frameCount - 1)
        seek(to: target)
    }

    public func seek(to frame: Int) {
        guard let m = movie, m.info.frameCount > 0 else { return }
        let target = min(max(frame, 0), m.info.frameCount - 1)
        previousPresentedFrame = nil
        store.clear()
        clearGPUFrameCache()
        if playing {
            if let ap = audioPlayer {
                ap.setLoop(loopEnabled)
                ap.play(fromFrame: frameAtAudio(target), rate: rateValue)
            } else {
                anchorWall(at: Double(target))
            }
        } else {
            audioPlayer?.park(atFrame: frameAtAudio(target))
            anchorWall(at: Double(target))
        }
        if let gpuFrame = metalFrames[target] {
            showMetalFrame(gpuFrame)
        } else if let img = store.get(target) {
            showFrame(target, image: img)
        } else {
            pendingScrubFrame = target
            kickFiller()
        }
        if !playing, let q = Self.pauseRefreshQuality() {
            scheduleFullQualityRefresh(quality: q)
        }
    }

    /// 秒跳转（⇧←/⇧→；mpv 引擎原生时间轴）。
    public func seekRelative(seconds: Double) {
        if let mpv {
            previousPresentedFrame = nil
            mpv.seekRelative(seconds)
            showOSD(LF("osd.seek", seconds < 0 ? "−" : "+", formatTime(abs(seconds))))
            return
        }
        seek(to: displayedFrame + Int(seconds * fps))
        showOSD(LF("osd.seek", seconds < 0 ? "−" : "+", formatTime(abs(seconds))))
    }

    /// mpv 模式拖进度条：绝对秒跳。
    public func seekSeconds(_ seconds: Double) {
        if let mpv {
            previousPresentedFrame = nil
            mpv.seekAbsolute(max(seconds, 0))
        } else if let movie, movie.info.fps > 0 {
            seek(to: Int(max(seconds, 0) * movie.info.fps))
        }
    }

    // MARK: - 倍速/循环/音量

    public func setRate(_ rate: Float) {
        rateValue = min(max(rate.isFinite ? rate : 1, 0.25), 4)
        let title = Self.rateTitle(rateValue)
        updateAccessibilityLabels()
        showOSD(LF("osd.rate", title))
        if let mpv {
            mpv.setSpeed(rateValue)
            return
        }
        if playing {
            if let ap = audioPlayer {
                ap.setRate(rateValue)
            } else {
                let f = clockMonotonicFrames()
                anchorWall(at: f)
            }
        }
    }

    @objc public func toggleLoop() {
        setLoop(!loopEnabled)
    }

    @objc private func togglePlaylistSidebar() {
        onTogglePlaylistSidebar?()
    }

    public func setLoop(_ loop: Bool) {
        loopEnabled = loop
        loopButton.contentTintColor = loop
            ? NSColor(calibratedRed: 0.35, green: 0.7, blue: 1.0, alpha: 1)
            : .white
        updateAccessibilityLabels()
        mpv?.setLoopFile(loop)
        audioPlayer?.setLoop(loop)
        showOSD(L(loop ? "osd.loopOn" : "osd.loopOff"))
    }

    func setVolumeLevel(_ volume: Float) {
        volumeValue = min(max(volume, 0), 2)
        muted = volumeValue == 0
        if UserDefaults.standard.bool(forKey: PreferenceKeys.rememberMute) {
            UserDefaults.standard.set(muted, forKey: PreferenceKeys.lastMuted)
        }
        volumeSlider.doubleValue = Double(volumeValue)
        applyVolumeToEngines()
        muteButton.image = NSImage(
            systemSymbolName: muted ? "speaker.slash" : "speaker.wave.2",
            accessibilityDescription: nil)
        updateAccessibilityLabels()
        showOSD(LF("osd.volume", Int(volumeValue * 100)))
    }

    func setAspectMode(_ mode: AspectMode) {
        aspectMode = mode
        updateLayoutFrames()
    }

    @objc public func rotateVideoClockwise(_ sender: Any?) {
        guard hasMedia else { return }
        rotationDegrees = (rotationDegrees + 90) % 360
        applyVideoTransform()
        showOSD(LF("osd.rotation", rotationDegrees))
    }

    @objc public func toggleHorizontalFlip(_ sender: Any?) {
        guard hasMedia else { return }
        flipHorizontal.toggle()
        applyVideoTransform()
        showOSD(L(flipHorizontal ? "menu.flipHorizontal" : "menu.flipHorizontalOff"))
    }

    @objc public func toggleVerticalFlip(_ sender: Any?) {
        guard hasMedia else { return }
        flipVertical.toggle()
        applyVideoTransform()
        showOSD(L(flipVertical ? "menu.flipVertical" : "menu.flipVerticalOff"))
    }

    var isHorizontallyFlipped: Bool { flipHorizontal }
    var isVerticallyFlipped: Bool { flipVertical }

    private func applyVideoTransform() {
        mpv?.setVideoRotation(rotationDegrees)
        mpv?.setVideoFlip(horizontal: flipHorizontal, vertical: flipVertical)
        metalVideoView?.setTransform(rotationDegrees: rotationDegrees,
                                     flipHorizontal: flipHorizontal,
                                     flipVertical: flipVertical)
        if movie != nil {
            if let gpuFrame = metalFrames[displayedFrame] {
                showMetalFrame(gpuFrame)
            } else if let image = store.get(displayedFrame) {
                showFrame(displayedFrame, image: image)
            } else {
                pendingScrubFrame = displayedFrame
                kickFiller()
            }
        }
        updateLayoutFrames()
    }

    private func resetVideoTransform() {
        rotationDegrees = 0
        flipHorizontal = false
        flipVertical = false
        mpv?.setVideoRotation(0)
        mpv?.setVideoFlip(horizontal: false, vertical: false)
        metalVideoView?.setTransform(rotationDegrees: 0,
                                     flipHorizontal: false,
                                     flipVertical: false)
    }

    @objc public func toggleMute() {
        muted.toggle()
        if UserDefaults.standard.bool(forKey: PreferenceKeys.rememberMute) {
            UserDefaults.standard.set(muted, forKey: PreferenceKeys.lastMuted)
        }
        applyVolumeToEngines()
        muteButton.image = NSImage(
            systemSymbolName: muted ? "speaker.slash" : "speaker.wave.2",
            accessibilityDescription: nil)
        updateAccessibilityLabels()
        showOSD(L(muted ? "osd.muted" : "osd.unmuted"))
    }

    @objc private func volumeChanged() {
        volumeValue = Float(volumeSlider.doubleValue)
        muted = volumeValue == 0
        if UserDefaults.standard.bool(forKey: PreferenceKeys.rememberMute) {
            UserDefaults.standard.set(muted, forKey: PreferenceKeys.lastMuted)
        }
        applyVolumeToEngines()
        muteButton.image = NSImage(
            systemSymbolName: muted ? "speaker.slash" : "speaker.wave.2",
            accessibilityDescription: nil)
        updateAccessibilityLabels()
        showOSD(LF("osd.volume", Int(volumeValue * 100)))
    }

    private func applyVolumeToEngines() {
        mpv?.setMuted(muted)
        mpv?.setVolume(muted ? 0 : volumeValue)
        audioPlayer?.setVolume(muted ? 0 : volumeValue)
    }

    public func setAudioEqualizer(_ gains: [Float]) {
        audioEqualizerGains = (0..<AudioProcessingSettings.frequencies.count).map { index in
            index < gains.count ? min(max(gains[index], -12), 12) : 0
        }
        audioPlayer?.setEqualizer(audioEqualizerGains)
        mpv?.setAudioEqualizer(audioEqualizerGains)
    }

    @objc public func showAudioEffectsAction(_ sender: Any?) {
        onShowAudioEffects?()
    }

    // MARK: - 轨道 / 截图（mpv 引擎）

    public func cycleAudioTrack() { mpv?.cycleAudioTrack() }
    public func cycleSubtitleTrack() { mpv?.cycleSubtitleTrack() }

    /// 双字幕轨：次字幕循环切换/指定轨（mpv secondary-sid；Topos 引擎无轨概念，no-op）。
    public func cycleSecondarySubtitleTrack() {
        guard let mpv else { return }
        mpv.cycleSecondarySubtitleTrack()
        showOSD(mpv.getString("secondary-sid").flatMap { Int64($0) } == nil
            ? L("osd.secondarySubOff") : L("osd.secondarySubOn"))
    }

    public func selectSecondarySubtitleTrack(id: Int64?) {
        mpv?.selectTrack(type: "secondary-sub", id: id)
    }

    /// 次字幕当前选中轨 id（菜单勾选态；无 = nil）。
    public var mpvSecondarySubtitleID: Int64? {
        mpv?.getString("secondary-sid").flatMap(Int64.init)
    }
    private func adjustSubtitleDelay(by seconds: Double) {
        if let mpv {
            mpv.adjustSubtitleDelay(by: seconds)
            if let delay = mpv.getDouble("sub-delay") {
                showOSD(LF("osd.subtitleDelay", delay))
            }
        } else if let renderer = toposSubtitles {
            renderer.setDelay(renderer.delaySeconds + seconds)
            showOSD(LF("osd.subtitleDelay", renderer.delaySeconds))
            refreshToposSubtitleOverlay()
        }
    }

    // MARK: P2.4 A/V 延迟微调（[ / ] 步进 10ms，范围 ±200ms）

    /// 当前音轨延迟（秒；正值 = 音频推迟、画面超前）。
    public private(set) var audioDelaySeconds: Double = 0

    /// 步进音轨延迟：mpv 写 `audio-delay`；Topos 设节点起始偏移并在
    /// 下次 play/resume 生效。OSD 显示当前偏移。
    public func adjustAudioDelay(by delta: Double) {
        var delay = audioDelaySeconds + delta
        delay = min(max((delay * 1000).rounded() / 1000, -0.2), 0.2)
        audioDelaySeconds = delay
        if let mpv {
            mpv.setDouble("audio-delay", delay)
        } else if let ap = audioPlayer {
            ap.setAudioDelay(delay)
        }
        showOSD(LF("osd.audioDelay", Int(delay * 1000)))
    }

    @objc public func audioDelayBackAction(_ sender: Any?) {
        adjustAudioDelay(by: -0.01)
    }

    @objc public func audioDelayForwardAction(_ sender: Any?) {
        adjustAudioDelay(by: 0.01)
    }

    /// P2.3 重采样质量档位偏好（low / medium / high）。
    static func resampleQualityPreference() -> AVAudioQuality {
        switch UserDefaults.standard.string(forKey: PreferenceKeys.audioResampleQuality) {
        case "low": return .low
        case "high": return .max
        default: return .medium
        }
    }

    @objc public func markABLoopStartAction(_ sender: Any?) {
        guard isLoaded else { return }
        let start = playbackPositionSeconds
        abLoopStart = start
        abLoopEnd = nil
        mpv?.setABLoop(start: start, end: nil)
        showOSD(LF("osd.abStart", formatTime(start)))
    }

    @objc public func markABLoopEndAction(_ sender: Any?) {
        guard isLoaded, let start = abLoopStart else {
            showOSD(L("osd.abNeedStart"))
            return
        }
        let end = playbackPositionSeconds
        guard end > start else {
            showOSD(L("osd.abInvalidEnd"))
            return
        }
        abLoopEnd = end
        mpv?.setABLoop(start: start, end: end)
        showOSD(LF("osd.abRange", formatTime(start), formatTime(end)))
    }

    public func screenshot() {
        guard let path = filePath else { return }
        let frameIndex: Int
        if let movie {
            frameIndex = displayedFrame
        } else if let mpv {
            frameIndex = max(Int(mpv.position * max(mpv.fps, 1)), 0)
        } else {
            frameIndex = 0
        }
        let target = ScreenshotPreferences.makeTarget(for: path, frameIndex: frameIndex)
        if let mpv {
            do {
                try ScreenshotPreferences.prepareDirectory(for: target)
                let scopeStarted = ScreenshotPreferences.startSecurityScope(for: target)
                mpv.screenshot(to: target.url)
                if scopeStarted {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                        ScreenshotPreferences.stopSecurityScope(for: target, started: true)
                    }
                }
            } catch {
                showScreenshotError(error)
            }
            return
        }
        guard let movie else { return }
        let cachedImage = store.get(frameIndex)
        decodeQueue.async { [weak self] in
            do {
                let image = try cachedImage ?? movie.frame(at: frameIndex, quality: .full).image
                try ScreenshotPreferences.write(image, to: target)
            } catch {
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.showScreenshotError(error)
                }
            }
        }
    }

    private func showScreenshotError(_ error: Error) {
        let alert = NSAlert()
        alert.messageText = L("alert.screenshotFailed")
        alert.informativeText = error.localizedDescription
        if let window {
            alert.beginSheetModal(for: window)
        } else {
            alert.runModal()
        }
    }

    static func screenshotURL(for path: String, date: Date = Date()) -> URL {
        let stamp = Int64(date.timeIntervalSince1970 * 1000)
        if let source = URL(string: path),
           ["http", "https", "rtmp", "rtsp"].contains(source.scheme?.lowercased() ?? "") {
            let directory = FileManager.default.urls(for: .picturesDirectory,
                                                      in: .userDomainMask).first
                ?? FileManager.default.temporaryDirectory
            let component = source.deletingPathExtension().lastPathComponent
            let name = component.isEmpty ? (source.host ?? "ToposScreenshot") : component
            return directory.appendingPathComponent("\(name)_\(stamp).png")
        }
        let source = URL(fileURLWithPath: path)
        return source.deletingLastPathComponent()
            .appendingPathComponent("\(source.deletingPathExtension().lastPathComponent)_\(stamp).png")
    }

    // MARK: - 全屏 / 信息面板

    @objc public func toggleFullscreen() {
        window?.toggleFullScreen(nil)
    }

    @objc public func toggleInfoPanel() {
        infoVisible.toggle()
        infoPanel.isHidden = !infoVisible
        if infoVisible {
            presentationStatsStart = CACurrentMediaTime()
            presentationFrames = 0
            lastInfoRefreshAt = presentationStatsStart
            updateInfoContent()
            updateLayoutFrames()
        }
    }

    private func updateInfoContent() {
        var lines: [String] = []
        if let m = movie {
            lines.append("引擎  " + L("info.engine.topos"))
            lines.append(L("info.resolution") + "  \(m.info.width) × \(m.info.height)")
            lines.append(L("info.fps") + "  \(String(format: "%.3f", m.info.fps)) fps")
            lines.append(LF("info.framesLine", m.info.frameCount,
                            formatTime(Double(m.info.frameCount) / m.info.fps)))
            lines.append(L("info.pixelFormat") + "  \(Self.pixelFormatName(m.info.pixelFormat)) \(m.info.bitDepth)-bit")
            if m.info.hasAlpha {
                lines.append(L("info.alpha") + "  \(m.info.alphaPremultiplied ? L("info.alpha.premult") : "straight")")
            }
            lines.append(L("info.tier") + "  " + Self.tierName(m.info.tierId))
            if m.scalableBase {
                lines.append(L("info.preview") + "  " + L("info.scalable"))
            }
            if m.audioTracks.indices.contains(selectedAudioTrackIndex) {
                let t = m.audioTracks[selectedAudioTrackIndex]
                let codecName = t.isLPCM
                    ? "LPCM \(t.bitsPerSample)-bit\(t.isFloat32 ? " float" : "")"
                    : "AAC"
                lines.append(L("info.audio") + "  \(codecName) \(t.sampleRate)Hz "
                    + "\(t.channelCount)ch（\(t.sampleCount)）"
                    + (t.name.isEmpty ? "" : "「\(t.name)」"))
            } else {
                lines.append(L("info.audio") + "  " + L("audio.noTrack"))
            }
            let stats = m.performanceSnapshot
            lines.append(LF("info.decodeMs", Self.milliseconds(stats.averageDecodeMilliseconds)))
            if metalRenderingEnabled && m.info.pixelFormat <= 1 {
                lines.append(LF("info.metalUploadMs",
                                Self.milliseconds(stats.averageMetalUploadMilliseconds)))
                let gpuAverage = gpuConversionSamples > 0
                    ? gpuConversionMilliseconds / Double(gpuConversionSamples) : 0
                lines.append(LF("info.gpuConvertMs", Self.milliseconds(gpuAverage)))
            } else {
                lines.append(LF("info.cpuConvertMs",
                                Self.milliseconds(stats.averageCPUConvertMilliseconds)))
            }
            lines.append(LF("info.presentFPS", String(format: "%.1f", measuredPresentationFPS)))
            lines.append(LF("info.droppedFrames", estimatedDroppedFrames))
            if let ap = audioPlayer {
                lines.append(LF("info.outputLatency",
                                Self.milliseconds(ap.outputLatencyMilliseconds)))
            }
        } else if let m = mpv {
            lines.append("引擎  " + L("info.engine.mpv"))
            if let title = m.getString("media-title") {
                lines.append(L("info.title") + "  \(title)")
            }
            if m.width > 0 {
                lines.append(L("info.resolution") + "  \(m.width) × \(m.height)")
            }
            if m.fps > 0 {
                lines.append(L("info.fps") + "  \(String(format: "%.3f", m.fps)) fps")
            }
            if let estimated = m.getDouble("estimated-vf-fps"), estimated > 0 {
                lines.append(LF("info.mpvEstimatedFPS", String(format: "%.1f", estimated)))
            }
            let voDrops = Int(m.getDouble("frame-drop-count") ?? 0)
            let decoderDrops = Int(m.getDouble("decoder-frame-drop-count") ?? 0)
            lines.append(LF("info.voDrops", voDrops))
            lines.append(LF("info.decoderDrops", decoderDrops))
            if let vf = m.getString("video-format") {
                lines.append(L("info.video") + "  \(vf)")
            }
            if let hw = m.getString("hwdec-current"), hw != "no" {
                lines.append(L("info.hwdec") + "  \(hw)")
            }
            if let ac = m.getString("audio-codec-name") {
                let rate = m.getString("audio-params/samplerate")
                    .flatMap { Double($0) }
                    .map { " \(Int($0))Hz" } ?? ""
                lines.append(L("info.audio") + "  \(ac)\(rate)")
            }
            if m.duration > 0 {
                lines.append(L("info.duration") + "  \(formatTime(m.duration))")
            }
        } else {
            lines.append(L("info.noMedia"))
        }
        if let note = audioNote {
            lines.append("⚠︎  \(note)")
        }
        if let err = engineError {
            lines.append("⚠︎  \(err)")
        }
        if let note = decodeErrorNote {
            lines.append("⚠︎  \(note)")
        }
        infoLabel.stringValue = lines.joined(separator: "\n")
    }

    private static func pixelFormatName(_ pf: UInt8) -> String {
        switch pf {
        case 1: return "YUV 4:4:4"
        case 2: return "GBR"
        case 3: return "CFA (RAW)"
        default: return "YUV 4:2:2"
        }
    }

    private static func milliseconds(_ value: Double) -> String {
        String(format: "%.2f ms/frame", value.isFinite ? value : 0)
    }

    private static func tierName(_ id: UInt32) -> String {
        switch id {
        case UInt32(TC_TIER_PROXY): return "Topos 422 Proxy"
        case UInt32(TC_TIER_LT): return "Topos 422 LT"
        case UInt32(TC_TIER_STANDARD): return "Topos 422"
        case UInt32(TC_TIER_HQ): return "Topos 422 HQ"
        case UInt32(TC_TIER_4444): return "Topos 4444"
        case UInt32(TC_TIER_4444XQ): return "Topos 4444 XQ"
        case UInt32(TC_TIER_LP): return "Topos 422 LP"
        case UInt32(TC_TIER_RAW): return "Topos RAW"
        default: return LF("info.tier.unknown", Int(id))
        }
    }

    // MARK: - 进度条拖拽

    @objc private func scrubChanged() {
        guard isLoaded else { return }
        if !wasPlayingBeforeScrub {
            wasPlayingBeforeScrub = playing
        }
        if playing { pausePlayback() }
        if mpv != nil {
            seekSeconds(slider.doubleValue)
        } else {
            let target = Int(slider.doubleValue)
            pendingScrubFrame = target
            kickFiller()
        }
    }

    private func scrubEnded() {
        let resume = wasPlayingBeforeScrub
        wasPlayingBeforeScrub = false
        if resume {
            startPlayback()
        } else if let q = Self.pauseRefreshQuality() {
            scheduleFullQualityRefresh(quality: q)
        }
    }

    // MARK: - 菜单 action（响应链：主 App 菜单 nil target）

    @objc public func stepForwardAction(_ sender: Any?) { stepFrame(1) }
    @objc public func stepBackwardAction(_ sender: Any?) { stepFrame(-1) }
    @objc public func seekForwardAction(_ sender: Any?) { seekRelative(seconds: 1) }
    @objc public func seekBackwardAction(_ sender: Any?) { seekRelative(seconds: -1) }
    @objc public func toStartAction(_ sender: Any?) {
        if mpv != nil { seekSeconds(0) } else { seek(to: 0) }
    }
    @objc public func toEndAction(_ sender: Any?) {
        if let m = mpv { seekSeconds(m.duration - 0.05) } else { seek(to: frameCount - 1) }
    }
    @objc public func cycleAudioAction(_ sender: Any?) { cycleAudioTrack() }
    @objc public func cycleSubtitleAction(_ sender: Any?) { cycleSubtitleTrack() }

    @objc public func cycleSecondarySubtitleAction(_ sender: Any?) {
        cycleSecondarySubtitleTrack()
    }
    @objc public func loadSubtitleAction(_ sender: Any?) { onLoadSubtitles?([]) }
    @objc public func subtitleDelayBackAction(_ sender: Any?) {
        adjustSubtitleDelay(by: -subtitleDelayStep)
    }
    @objc public func subtitleDelayForwardAction(_ sender: Any?) {
        adjustSubtitleDelay(by: subtitleDelayStep)
    }
    @objc public func toggleSubtitleVisibilityAction(_ sender: Any?) {
        if mpv == nil, let renderer = toposSubtitles {
            renderer.setVisibility(!renderer.isVisible)
            refreshToposSubtitleOverlay()
            showOSD(renderer.isVisible ? L("subtitles.visible") : L("subtitles.hidden"))
            return
        }
        mpv?.toggleSubtitleVisibility()
    }
    @objc public func screenshotAction(_ sender: Any?) { screenshot() }
    @objc public func copyScreenshotToClipboardAction(_ sender: Any?) {
        copyScreenshotToClipboard()
    }
    @objc public func toggleTimecodeAction(_ sender: Any?) {
        timecodeEnabled.toggle()
        updateHud()
        if infoVisible { updateInfoContent() }
        showOSD(L("menu.timecode"))
    }

    private func copyScreenshotToClipboard() {
        guard isLoaded else { return }
        let generation = mediaGeneration
        if let movie {
            let frameIndex = displayedFrame
            decodeQueue.async { [weak self] in
                let image = try? movie.frame(at: frameIndex, quality: .full).image
                DispatchQueue.main.async { [weak self] in
                    guard let self, self.mediaGeneration == generation else { return }
                    if let image { self.writeClipboardImage(image) }
                    else { self.showScreenshotError(ToposCodecError.unsupported("截图帧解码失败")) }
                }
            }
            return
        }
        guard let mpv else { return }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("topos-player-clipboard-\(UUID().uuidString).png")
        mpv.screenshot(to: url)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var image: CGImage?
            for _ in 0..<100 {
                if let source = CGImageSourceCreateWithURL(url as CFURL, nil),
                   let decoded = CGImageSourceCreateImageAtIndex(source, 0, nil) {
                    image = decoded
                    break
                }
                Thread.sleep(forTimeInterval: 0.05)
            }
            try? FileManager.default.removeItem(at: url)
            DispatchQueue.main.async { [weak self] in
                guard let self, self.mediaGeneration == generation else { return }
                if let image { self.writeClipboardImage(image) }
                else { self.showScreenshotError(ToposCodecError.unsupported("mpv 截图未能生成")) }
            }
        }
    }

    private func writeClipboardImage(_ image: CGImage) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        let object = NSImage(cgImage: image,
                             size: NSSize(width: image.width, height: image.height))
        guard pasteboard.writeObjects([object]) else {
            showScreenshotError(ToposCodecError.unsupported("无法写入剪贴板"))
            return
        }
        showOSD(L("osd.copied"))
    }

    public func addSubtitle(path: String) {
        if let mpv {
            mpv.addSubtitle(path: path)
        } else if let renderer = toposSubtitles {
            if renderer.load(file: path) {
                refreshToposSubtitleOverlay()
            } else {
                showOSD(L("subtitles.loadFailed"))
            }
        }
    }

    private var subtitleDelayStep: Double {
        let configured = UserDefaults.standard.double(forKey: PreferenceKeys.subtitleDelayStep)
        return configured.isFinite && configured > 0 ? min(configured, 5) : 0.5
    }

    @objc public func speedAction(_ sender: NSMenuItem) {
        let r = Self.playbackRates[min(max(sender.tag, 0), Self.playbackRates.count - 1)]
        setRate(r)
    }

    public override func scrollWheel(with event: NSEvent) {
        guard hasMedia, event.deltaY != 0 else {
            super.scrollWheel(with: event)
            return
        }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if flags.contains(.shift) {
            seekRelative(seconds: event.deltaY > 0 ? 5 : -5)
        } else {
            volumeValue = min(max(volumeValue + (event.deltaY > 0 ? 0.05 : -0.05), 0), 2)
            volumeSlider.doubleValue = Double(volumeValue)
            volumeChanged()
        }
    }

    // MARK: - 键盘

    public override var acceptsFirstResponder: Bool { true }

    public override func menu(for event: NSEvent) -> NSMenu? {
        buildContextMenu()
    }

    @objc func pictureInPictureAction(_ sender: Any?) {
        onPictureInPictureRequested?()
    }

    public override func keyDown(with event: NSEvent) {
        guard isLoaded else {
            super.keyDown(with: event)
            return
        }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let characters = event.charactersIgnoringModifiers?.lowercased() ?? ""
        if flags.contains(.command) && flags.contains(.shift) && characters == "s" {
            copyScreenshotToClipboard()
            return
        }
        if flags.contains(.option) && (characters == "+" || characters == "=") {
            setRate(rateValue + 0.05)
            return
        }
        if flags.contains(.option) && characters == "-" {
            setRate(rateValue - 0.05)
            return
        }
        let special = event.specialKey
        if special == .leftArrow {
            if flags.contains(.shift) {
                seekRelative(seconds: -1)
            } else if flags.contains(.option) {
                stepFrame(-10)
            } else {
                stepFrame(-1)
            }
        } else if special == .rightArrow {
            if flags.contains(.shift) {
                seekRelative(seconds: 1)
            } else if flags.contains(.option) {
                stepFrame(10)
            } else {
                stepFrame(1)
            }
        } else if special == .home || special == .pageUp {
            if mpv != nil { seekSeconds(0) } else { seek(to: 0) }
        } else if special == .end || special == .pageDown {
            if let m = mpv { seekSeconds(m.duration - 0.05) } else { seek(to: frameCount - 1) }
        } else if special == .upArrow {
            volumeValue = min(volumeValue + 0.1, 2)
            volumeSlider.doubleValue = Double(volumeValue)
            volumeChanged()
        } else if special == .downArrow {
            volumeValue = max(volumeValue - 0.1, 0)
            volumeSlider.doubleValue = Double(volumeValue)
            volumeChanged()
        } else {
            // P3.1：自定义键位优先（动作表匹配 → 响应链执行）；
            // 未命中再走内置键表。
            let combo = KeyCombo(characters: characters, modifiers: flags.rawValue)
            if let action = KeymapManager.shared.action(
                for: combo,
                isMPVEngine: supportsTrackSelection,
                hasMedia: hasMedia) {
                _ = NSApp.sendAction(action.selector, to: nil, from: self)
                return
            }
            switch event.charactersIgnoringModifiers {
            case " ", "　": togglePlay()
            case "m": toggleMute()
            case "l": toggleLoop()
            case "f": toggleFullscreen()
            case "i": markABLoopStartAction(nil)
            case "o": markABLoopEndAction(nil)
            case "+", "=": bumpRate(up: true)
            case "-": bumpRate(up: false)
            case "a": cycleAudioTrack()   // mpv：切换音轨
            case "c": cycleSubtitleTrack() // mpv：切换字幕
            case "z": adjustSubtitleDelay(by: -subtitleDelayStep)
            case "x": adjustSubtitleDelay(by: subtitleDelayStep)
            case "[": adjustAudioDelay(by: -0.01)  // P2.4：音轨延迟 −10ms
            case "]": adjustAudioDelay(by: 0.01)   // P2.4：音轨延迟 +10ms
            case "s": screenshot()         // mpv：截图到源文件目录
            default:
                super.keyDown(with: event)
            }
        }
    }

    private func bumpRate(up: Bool) {
        let idx = Self.playbackRates.firstIndex(of: rateValue) ?? 3
        let next = min(max(idx + (up ? 1 : -1), 0), Self.playbackRates.count - 1)
        setRate(Self.playbackRates[next])
    }

    static func rateTitle(_ r: Float) -> String {
        if r.rounded() == r { return "\(Int(r))×" }
        var value = String(format: "%.2f", locale: Locale(identifier: "en_US_POSIX"), r)
        while value.last == "0" { value.removeLast() }
        if value.last == "." { value.removeLast() }
        return "\(value)×"
    }

    // MARK: - 鼠标 / 控制栏自动隐藏

    public override func mouseUp(with event: NSEvent) {
        if event.modifierFlags.contains(.control) {
            super.mouseUp(with: event)
        } else if event.clickCount == 2 {
            toggleFullscreen()
        } else {
            super.mouseUp(with: event)
        }
    }

    public override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for area in trackingAreas { removeTrackingArea(area) }
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways],
            owner: self, userInfo: nil))
    }

    public override func mouseMoved(with event: NSEvent) {
        showControls()
        scheduleControlsHide()
        updateHoverPreview(at: convert(event.locationInWindow, from: nil))
    }

    public override func mouseExited(with event: NSEvent) {
        hideHoverPreview()
    }

    private func updateHoverPreview(at point: NSPoint) {
        guard hasMedia, controlsVisible, !controlsBar.isHidden,
              NSPointInRect(controlsBar.convert(point, from: self), slider.frame),
              slider.frame.width > 1 else {
            hideHoverPreview()
            return
        }

        let sliderPoint = controlsBar.convert(point, from: self)
        let fraction = min(max(Double((sliderPoint.x - slider.frame.minX)
                                    / slider.frame.width), 0), 1)
        let duration: Double
        let sourceMovie = movie
        if let movie {
            duration = Double(movie.info.frameCount) / max(movie.info.fps, 1)
        } else if let mpv {
            duration = mpv.duration
        } else {
            hideHoverPreview()
            return
        }
        guard duration.isFinite, duration > 0 else {
            hideHoverPreview()
            return
        }

        let targetTime = min(max(duration * fraction, 0), max(duration - 0.001, 0))
        previewGeneration &+= 1
        let generation = previewGeneration
        previewWorkItem?.cancel()
        let sourcePath = filePath
        let work = DispatchWorkItem { [weak self, sourceMovie, sourcePath] in
            let image = Self.previewImage(movie: sourceMovie, path: sourcePath,
                                          time: targetTime)
            guard let image else { return }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.previewGeneration == generation else { return }
                self.previewImageView.image = NSImage(cgImage: image,
                                                      size: NSSize(width: image.width,
                                                                   height: image.height))
                self.previewTimeLabel.stringValue = self.formatTime(targetTime)
                let panelSize = self.previewPanel.frame.size
                let x = min(max(point.x - panelSize.width / 2, 8),
                            max(self.bounds.width - panelSize.width - 8, 8))
                let y = min(self.controlsBar.frame.maxY + 8,
                            max(self.bounds.height - panelSize.height - 8, 8))
                self.previewPanel.setFrameOrigin(NSPoint(x: x, y: y))
                self.previewPanel.isHidden = false
            }
        }
        previewWorkItem = work
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 0.08,
                                                              execute: work)
    }

    private static func previewImage(movie: ToposMovie?, path: String?,
                                     time: Double) -> CGImage? {
        // P2.5：缩略图统一走 SeekPreviewCache（键=路径+大小+mtime）；
        // Topos 用 reduced 四分之一解码（比 auto2k 更轻），mpv 格式走
        // headless mpv 截图（mkv/flv 等不再受 AVAssetImageGenerator 盲区）。
        let bucket: Int
        let cached: CGImage?
        if let movie {
            bucket = Int(time * movie.info.fps)
        } else {
            bucket = Int(time)
        }
        if let path, let fileKey = SeekPreviewCache.key(for: path) {
            cached = SeekPreviewCache.shared.image(for: fileKey, bucket: bucket)
            if let cached { return cached }
        }
        let image = Self.decodePreviewImage(movie: movie, path: path, time: time)
        if let image, let path, let fileKey = SeekPreviewCache.key(for: path) {
            SeekPreviewCache.shared.store(image, for: fileKey, bucket: bucket)
        }
        return image
    }

    private static func decodePreviewImage(movie: ToposMovie?, path: String?,
                                           time: Double) -> CGImage? {
        if let movie {
            let frame = min(max(Int(time * movie.info.fps), 0),
                            max(movie.info.frameCount - 1, 0))
            return try? movie.reducedFrame(
                at: frame, scale: UInt32(TC_DECODE_SCALE_QUARTER.rawValue)).image
        }
        guard let path else { return nil }
        let url: URL
        if let candidate = URL(string: path),
           let scheme = candidate.scheme?.lowercased(),
           ["http", "https", "rtsp", "rtmp"].contains(scheme) {
            url = candidate
        } else {
            url = URL(fileURLWithPath: path)
        }
        if url.isFileURL,
           let image = MPVThumbnailProvider.shared.thumbnail(path: path, time: time) {
            return image
        }
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 424, height: 232)
        let timestamp = CMTime(seconds: time, preferredTimescale: 600)
        generator.requestedTimeToleranceBefore = CMTime(value: 1, timescale: 5)
        generator.requestedTimeToleranceAfter = CMTime(value: 1, timescale: 5)
        return try? generator.copyCGImage(at: timestamp, actualTime: nil)
    }

    private func hideHoverPreview() {
        previewGeneration &+= 1
        previewWorkItem?.cancel()
        previewWorkItem = nil
        previewPanel.isHidden = true
        previewImageView.image = nil
    }

    private func scheduleControlsHide() {
        hideWorkItem?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self, self.playing else { return }
            self.hideControls()
        }
        hideWorkItem = item
        let delay = min(max(UserDefaults.standard.double(
            forKey: PreferenceKeys.controlsHideDelay), 0.25), 30)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }

    private func showControls() {
        guard !controlsVisible else { return }
        controlsVisible = true
        controlsBar.isHidden = false
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.2
            controlsBar.animator().alphaValue = 1
        }
    }

    private func hideControls() {
        guard controlsVisible else { return }
        hideHoverPreview()
        controlsVisible = false
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.25
            controlsBar.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            if let self, !self.controlsVisible { self.controlsBar.isHidden = true }
        })
    }

    // MARK: - 拖拽打开

    public override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        let urls = Self.draggedURLs(sender)
        return urls.isEmpty ? [] : .copy
    }

    public override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = Self.draggedURLs(sender)
        guard !urls.isEmpty else { return false }
        let subtitleURLs = urls.filter { Self.subtitleExtensions.contains($0.pathExtension.lowercased()) }
        if supportsTrackSelection, let onLoadSubtitles,
           !subtitleURLs.isEmpty, subtitleURLs.count == urls.count {
            onLoadSubtitles(subtitleURLs)
            return true
        }
        if let onOpenFiles {
            onOpenFiles(urls)
        } else if let url = urls.first {
            onOpenFile?(url)
        }
        return true
    }

    /// 可播放扩展名（Topos 官方 + mpv 长尾）。
    public static let playableExtensions: Set<String> = [
        "topos", "toos",
        "mp4", "m4v", "mov", "mkv", "avi", "webm", "flv", "wmv", "ts", "m2ts",
        "mpg", "mpeg", "vob", "3gp", "ogv", "rmvb", "rm", "asf", "divx",
        "mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma", "ape",
    ]

    public static let subtitleExtensions: Set<String> = ["srt", "ass", "ssa", "vtt", "sub"]

    private static func draggedURLs(_ sender: NSDraggingInfo) -> [URL] {
        guard let items = sender.draggingPasteboard
            .readObjects(forClasses: [NSURL.self],
                         options: [.urlReadingFileURLsOnly: true]) as? [URL],
            !items.isEmpty else { return [] }
        return items
    }

    // MARK: - 布局

    @objc private func layoutFrames() { updateLayoutFrames() }

    private func updateLayoutFrames() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        let videoRect: NSRect
        switch aspectMode {
        case .ratio1x1, .ratio4x3, .ratio16x9:
            if let ratio = aspectMode.fixedRatio, bounds.width > 0, bounds.height > 0 {
                let boxRatio = bounds.width / bounds.height
                if boxRatio > ratio {
                    let width = bounds.height * ratio
                    videoRect = NSRect(x: bounds.midX - width / 2, y: bounds.minY,
                                       width: width, height: bounds.height)
                } else {
                    let height = bounds.width / ratio
                    videoRect = NSRect(x: bounds.minX, y: bounds.midY - height / 2,
                                       width: bounds.width, height: height)
                }
            } else {
                videoRect = bounds
            }
        default:
            videoRect = bounds
        }
        switch aspectMode {
        case .stretch:
            imageLayer.contentsGravity = .resize
            layer?.masksToBounds = false
        case .cropFill:
            imageLayer.contentsGravity = .resizeAspectFill
            layer?.masksToBounds = true
        default:
            imageLayer.contentsGravity = .resizeAspect
            layer?.masksToBounds = false
        }
        imageLayer.frame = videoRect
        metalVideoView?.frame = videoRect
        metalVideoView?.setAspectMode(aspectMode)
        pictureInPictureLayer?.frame = videoRect
        mpvGLView?.frame = videoRect
        let viewportAspect = bounds.height > 0 ? Double(bounds.width / bounds.height) : nil
        mpv?.setAspectMode(aspectMode.rawValue, viewportAspect: viewportAspect)
        let barHeight: CGFloat = 48
        controlsBar.frame = NSRect(x: 0, y: 0, width: bounds.width, height: barHeight)

        var x: CGFloat = 12
        playButton.frame = NSRect(x: x, y: 10, width: 34, height: 28)
        x += 40
        timeLabel.sizeToFit()
        let timeW = timeLabel.fittingSize.width + 8
        timeLabel.frame = NSRect(x: x, y: 15, width: timeW, height: 16)
        x += timeW + 8

        // 右侧组：播放列表/循环/静音/音量/全屏（从右往左排）
        var right = bounds.width - 12
        fullscreenButton.frame = NSRect(x: right - 24, y: 12, width: 24, height: 24)
        right -= 32
        volumeSlider.frame = NSRect(x: right - 64, y: 16, width: 64, height: 18)
        right -= 70
        muteButton.frame = NSRect(x: right - 22, y: 12, width: 22, height: 24)
        right -= 28
        loopButton.frame = NSRect(x: right - 22, y: 12, width: 22, height: 24)
        right -= 28
        if !playlistButton.isHidden {
            playlistButton.frame = NSRect(x: right - 24, y: 12, width: 24, height: 24)
            right -= 32
        }

        slider.frame = NSRect(x: x, y: 16, width: max(right - x, 40), height: 20)

        infoLabel.sizeToFit()
        let infoSize = infoLabel.fittingSize
        infoPanel.frame = NSRect(x: bounds.width - infoSize.width - 32,
                                 y: bounds.height - infoSize.height - 40,
                                 width: infoSize.width + 24,
                                 height: infoSize.height + 16)
        infoLabel.frame = NSRect(x: 12, y: 8, width: infoSize.width, height: infoSize.height)
        osdLabel.sizeToFit()
        let osdSize = osdLabel.fittingSize
        let osdWidth = osdSize.width + 32
        let osdHeight = max(osdSize.height + 18, 42)
        osdPanel.frame = NSRect(x: (bounds.width - osdWidth) / 2,
                                y: bounds.midY - osdHeight / 2,
                                width: osdWidth, height: osdHeight)
        osdLabel.frame = NSRect(x: 12, y: (osdHeight - osdSize.height) / 2,
                                width: osdWidth - 24, height: osdSize.height)
        CATransaction.commit()
    }

    public override func layout() {
        super.layout()
        updateLayoutFrames()
    }
}

// MARK: - 图片视图

public final class ToposImageView: NSView {
    private let imageLayer = CALayer()
    private var zoom: CGFloat = 1
    private var pan = CGPoint.zero
    private var dragStart: CGPoint?
    private var imageAspect: CGFloat = 1

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        commonInit()
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        commonInit()
    }

    private func commonInit() {
        wantsLayer = true
        layer = CALayer()
        layer?.backgroundColor = NSColor(calibratedWhite: 0.1, alpha: 1).cgColor
        imageLayer.contentsGravity = .resizeAspect
        imageLayer.shadowColor = NSColor.black.cgColor
        imageLayer.shadowOpacity = 0.6
        imageLayer.shadowOffset = CGSize(width: 0, height: -4)
        layer?.addSublayer(imageLayer)

        postsFrameChangedNotifications = true
        NotificationCenter.default.addObserver(
            self, selector: #selector(relayout),
            name: NSView.frameDidChangeNotification, object: self)
    }

    public func load(path: String, maximumSize: CGSize) throws {
        let file = try ToposImageFile(path: path)
        let image = try file.image(maximumSize: maximumSize)
        imageAspect = CGFloat(image.width) / CGFloat(image.height)
        imageLayer.contents = image
        relayoutNow()
    }

    public override func scrollWheel(with event: NSEvent) {
        let factor = event.deltaY > 0 ? 0.9 : 1.1
        zoom = min(max(zoom * factor, 0.1), 20)
        relayoutNow()
    }

    public override func mouseDown(with event: NSEvent) {
        dragStart = NSEvent.mouseLocation
    }

    public override func mouseDragged(with event: NSEvent) {
        guard let start = dragStart else { return }
        let now = NSEvent.mouseLocation
        pan = CGPoint(x: pan.x + (now.x - start.x), y: pan.y - (now.y - start.y))
        dragStart = now
        relayoutNow()
    }

    public override func mouseUp(with event: NSEvent) {
        if event.clickCount == 2 {
            zoom = zoom > 1.001 ? 1 : 2  // 双击：100% ↔ 适合窗口
            relayoutNow()
        }
        dragStart = nil
    }

    @objc private func relayout() { relayoutNow() }

    private func relayoutNow() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        let fit = CGRect(origin: .zero, size: bounds.size)
        let frame: CGRect
        if bounds.width / max(bounds.height, 1) > imageAspect {
            let h = fit.height
            let w = h * imageAspect
            frame = CGRect(x: (bounds.width - w) / 2, y: 0, width: w, height: h)
        } else {
            let w = fit.width
            let h = w / imageAspect
            frame = CGRect(x: 0, y: (bounds.height - h) / 2, width: w, height: h)
        }
        let z = frame.insetBy(dx: (frame.width * (zoom - 1)) / 2,
                              dy: (frame.height * (zoom - 1)) / 2)
        imageLayer.frame = z.offsetBy(dx: pan.x, dy: pan.y)
        CATransaction.commit()
    }

    public override func layout() {
        super.layout()
        relayoutNow()
    }
}
