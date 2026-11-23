// MPVGLView——libmpv render API 的 NSOpenGLView 宿主。
//
// mpv 经 mpv_render_context_render 直绘进本视图的 GL 上下文（fbo 0）；
// update 回调只置 needsDisplay，真正的绘制走 AppKit draw(_:) 主线程。
// OpenGL 在 macOS 15 仍可用（已弃用未移除）——mpv 官方 macOS 路径
// （IINA 同款）就是 OpenGL render API。

import AppKit
import CoreGraphics

private typealias MPVGLReadPixels = @convention(c) (
    Int32, Int32, Int32, Int32, UInt32, UInt32, UnsafeMutableRawPointer?
) -> Void

final class MPVGLView: NSOpenGLView {
    private var renderCtx: OpaquePointer?
    var pictureInPictureCaptureEnabled = false
    var onPictureInPictureFrame: ((CGImage, Double, Double) -> Void)?
    var currentPosition: (() -> Double)?
    var duration: (() -> Double)?
    private var lastPictureInPictureCapture = 0.0

    private static let readPixels: MPVGLReadPixels? = {
        let name = "glReadPixels" as CFString
        let bundle = CFBundleGetBundleWithIdentifier("com.apple.opengl" as CFString)
        let bundleSymbol = bundle.flatMap {
            CFBundleGetFunctionPointerForName($0, name)
        }
        guard let symbol = bundleSymbol
                ?? dlsym(UnsafeMutableRawPointer(bitPattern: -2), "glReadPixels") else {
            return nil
        }
        return unsafeBitCast(symbol, to: MPVGLReadPixels.self)
    }()

    private static let updateCallback: @convention(c) (UnsafeMutableRawPointer?) -> Void = { ctx in
        guard let ctx else { return }
        let view = Unmanaged<MPVGLView>.fromOpaque(ctx).takeUnretainedValue()
        DispatchQueue.main.async {
            view.needsDisplay = true
        }
    }

    /// 自定义可失败 designated init（父类 init(frame:pixelFormat:) 可失败）。
    init?(videoFrame frameRect: NSRect) {
        let attrs: [NSOpenGLPixelFormatAttribute] = [
            UInt32(NSOpenGLPFAOpenGLProfile),
            UInt32(NSOpenGLProfileVersion3_2Core),
            UInt32(NSOpenGLPFADoubleBuffer),
            0,
        ]
        guard let pf = NSOpenGLPixelFormat(attributes: attrs) else { return nil }
        super.init(frame: frameRect, pixelFormat: pf)
        // 立即建上下文（NSOpenGLView 默认懒到上窗口才建——attach 需要它）
        if let ctx = NSOpenGLContext(format: pf, share: nil) {
            openGLContext = ctx
            ctx.makeCurrentContext()
        }
        autoresizingMask = [.width, .height]
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) 不支持")
    }

    /// 在 mpv_initialize 之后调用。失败 → false（调用方回退提示）。
    func attach(to core: MPVCore) -> Bool {
        guard let h = core.mpvHandle, !core.destroyed,
              let ctx = openGLContext else {
            return false
        }
        ctx.makeCurrentContext()

        // macOS GL 函数取址：OpenGL 框架 CFBundle（IINA 同款）+ dlsym 兜底
        let getProc: MPVLib.GetProcAddressFn = { _, name in
            guard let name else { return nil }
            let oglBundle = CFBundleGetBundleWithIdentifier("com.apple.opengl" as CFString)
            if let bundle = oglBundle,
               let sym = CFBundleGetFunctionPointerForName(
                   bundle, String(cString: name) as CFString) {
                return sym
            }
            return dlsym(UnsafeMutableRawPointer(bitPattern: -2), name) // RTLD_DEFAULT
        }
        var initParams = MPVLib.OpenGLInitParams(
            getProcAddress: getProc, ctx: nil)
        let apiType = strdup(MPVLib.renderAPITypeOpenGL)
        defer { free(apiType) }
        var newCtx: OpaquePointer?
        var rc: Int32 = -1
        // 参数结构体指针必须在 renderContextCreate 调用期间有效——
        // withUnsafeMutablePointer 包裹（数组字面量里 &struct 会立刻失效）
        withUnsafeMutablePointer(to: &initParams) { ip in
            var params = [
                MPVLib.RenderParam(type: MPVLib.RenderParamType.API_TYPE,
                                   data: UnsafeMutableRawPointer(apiType)),
                MPVLib.RenderParam(type: MPVLib.RenderParamType.OPENGL_INIT_PARAMS,
                                   data: ip),
                MPVLib.RenderParam(type: 0, data: nil),
            ]
            rc = params.withUnsafeMutableBufferPointer { buf in
                MPVLib.renderContextCreate(&newCtx, h,
                                           UnsafeMutableRawPointer(buf.baseAddress))
            }
        }
        guard rc == 0, let renderCtxNew = newCtx else { return false }
        renderCtx = renderCtxNew
        MPVLib.renderContextSetUpdateCallback(
            renderCtxNew, Self.updateCallback,
            Unmanaged.passUnretained(self).toOpaque())
        needsDisplay = true
        return true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let renderCtx else { return }
        openGLContext?.makeCurrentContext()
        let scale = window?.backingScaleFactor ?? 2
        let w = max(Int32(bounds.width * scale), 1)
        let h = max(Int32(bounds.height * scale), 1)
        var fbo = MPVLib.OpenGLFBO(fbo: 0, w: w, h: h, internalFormat: 0)
        var flip: Int32 = 1
        withUnsafeMutablePointer(to: &fbo) { fp in
            withUnsafeMutablePointer(to: &flip) { flp in
                var params = [
                    MPVLib.RenderParam(type: MPVLib.RenderParamType.OPENGL_FBO,
                                       data: fp),
                    MPVLib.RenderParam(type: MPVLib.RenderParamType.FLIP_Y,
                                       data: flp),
                    MPVLib.RenderParam(type: 0, data: nil),
                ]
                _ = params.withUnsafeMutableBufferPointer { buf in
                    MPVLib.renderContextRender(
                        renderCtx, UnsafeMutableRawPointer(buf.baseAddress))
                }
            }
        }
        capturePictureInPictureFrame(width: w, height: h)
        openGLContext?.flushBuffer()
        playbackDiagnosticDidDraw()
    }

    private func capturePictureInPictureFrame(width: Int32, height: Int32) {
        guard pictureInPictureCaptureEnabled,
              let readPixels = Self.readPixels,
              width > 0, height > 0 else { return }
        let now = CACurrentMediaTime()
        guard now - lastPictureInPictureCapture >= 1.0 / 12.0 else { return }
        lastPictureInPictureCapture = now
        let rowBytes = Int(width) * 4
        var pixels = Data(count: rowBytes * Int(height))
        pixels.withUnsafeMutableBytes { rawBuffer in
            guard let baseAddress = rawBuffer.baseAddress else { return }
            // Read the just-rendered default framebuffer. GL_RGBA / GL_UNSIGNED_BYTE
            // gives a stable byte layout for CGImage on both supported architectures.
            readPixels(0, 0, width, height, 0x1908, 0x1401, baseAddress)
        }
        guard let provider = CGDataProvider(data: pixels as CFData),
              let image = CGImage(
                width: Int(width), height: Int(height),
                bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: rowBytes,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGBitmapInfo.byteOrder32Big.union(
                    CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue)),
                provider: provider, decode: nil,
                shouldInterpolate: true, intent: .defaultIntent) else { return }
        onPictureInPictureFrame?(image, currentPosition?() ?? 0, duration?() ?? 0)
    }

    /// 必须在销毁 mpv handle 之前调用（render ctx 依赖 client 存活）。
    func detach() {
        if let renderCtx {
            MPVLib.renderContextSetUpdateCallback(renderCtx, nil, nil)
            MPVLib.renderContextFree(renderCtx)
        }
        renderCtx = nil
    }
}
