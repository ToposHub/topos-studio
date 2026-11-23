import AppKit
import MetalKit
import simd

/// Metal display surface for decoded Topos YUV frames. CPU-backed CGImage
/// rendering remains available for Quick Look and screenshots.
final class ToposMetalVideoView: MTKView, MTKViewDelegate {
    private struct Uniforms {
        var dimensions: SIMD4<UInt32>
        var flags: SIMD4<UInt32>
        var aspect: SIMD4<Float>
        // P5.1 色彩管理：x=使能 y=源 EOTF(0=709视频,1=sRGB,2=线性) z/w 保留
        var colorConvert: SIMD4<Float>
        // 行优先 3×3 源线性RGB→显示线性RGB
        var cmsRow0: SIMD4<Float>
        var cmsRow1: SIMD4<Float>
        var cmsRow2: SIMD4<Float>
    }

    private static let shaderSource = #"""
#include <metal_stdlib>
using namespace metal;

struct VertexOut {
    float4 position [[position]];
    float2 uv;
};

struct Uniforms {
    uint4 dimensions; // width, height, bit depth, U/V width
    uint4 flags;      // matrix, range, pixel format, alpha flags
    float4 aspect;    // view aspect, source aspect, mode, reserved
};

vertex VertexOut topoplayer_vertex(uint id [[vertex_id]]) {
    constexpr float2 positions[6] = {
        float2(-1.0,  1.0), float2( 1.0,  1.0), float2(-1.0, -1.0),
        float2(-1.0, -1.0), float2( 1.0,  1.0), float2( 1.0, -1.0)
    };
    constexpr float2 texcoords[6] = {
        float2(0.0, 0.0), float2(1.0, 0.0), float2(0.0, 1.0),
        float2(0.0, 1.0), float2(1.0, 0.0), float2(1.0, 1.0)
    };
    VertexOut out;
    out.position = float4(positions[id], 0.0, 1.0);
    out.uv = texcoords[id];
    return out;
}

fragment float4 topoplayer_fragment(
    VertexOut in [[stage_in]],
    texture2d<ushort, access::read> yPlane [[texture(0)]],
    texture2d<ushort, access::read> uPlane [[texture(1)]],
    texture2d<ushort, access::read> vPlane [[texture(2)]],
    texture2d<ushort, access::read> aPlane [[texture(3)]],
    constant Uniforms& p [[buffer(0)]]) {
    float2 uv = in.uv;
    const uint transform = uint(p.aspect.w + 0.5);
    const float viewAspect = max(p.aspect.x, 0.0001);
    float sourceAspect = max(p.aspect.y, 0.0001);
    const uint mode = uint(p.aspect.z + 0.5); // 0 fit, 1 stretch, 2 crop
    if (mode != 1) {
        if (mode == 2) {
            if (viewAspect > sourceAspect) {
                const float keep = sourceAspect / viewAspect;
                if (uv.y < (1.0 - keep) * 0.5 || uv.y > 1.0 - (1.0 - keep) * 0.5)
                    return float4(0.0, 0.0, 0.0, 1.0);
                uv.y = (uv.y - (1.0 - keep) * 0.5) / keep;
            } else {
                const float keep = viewAspect / sourceAspect;
                if (uv.x < (1.0 - keep) * 0.5 || uv.x > 1.0 - (1.0 - keep) * 0.5)
                    return float4(0.0, 0.0, 0.0, 1.0);
                uv.x = (uv.x - (1.0 - keep) * 0.5) / keep;
            }
        } else if (viewAspect > sourceAspect) {
            const float keep = sourceAspect / viewAspect;
            if (uv.x < (1.0 - keep) * 0.5 || uv.x > 1.0 - (1.0 - keep) * 0.5)
                return float4(0.0, 0.0, 0.0, 1.0);
            uv.x = (uv.x - (1.0 - keep) * 0.5) / keep;
        } else {
            const float keep = viewAspect / sourceAspect;
            if (uv.y < (1.0 - keep) * 0.5 || uv.y > 1.0 - (1.0 - keep) * 0.5)
                return float4(0.0, 0.0, 0.0, 1.0);
            uv.y = (uv.y - (1.0 - keep) * 0.5) / keep;
        }
    }

    switch (transform & 3) {
        case 1: uv = float2(uv.y, 1.0 - uv.x); break;
        case 2: uv = float2(1.0 - uv.x, 1.0 - uv.y); break;
        case 3: uv = float2(1.0 - uv.y, uv.x); break;
        default: break;
    }
    if ((transform & 4) != 0) uv.x = 1.0 - uv.x;
    if ((transform & 8) != 0) uv.y = 1.0 - uv.y;

    const uint width = p.dimensions.x;
    const uint height = p.dimensions.y;
    const uint bitDepth = p.dimensions.z;
    const uint chromaWidth = p.dimensions.w;
    const uint x = min(uint(clamp(uv.x, 0.0, 0.99999994) * float(width)), width - 1);
    const uint y = min(uint(clamp(uv.y, 0.0, 0.99999994) * float(height)), height - 1);
    const ushort yCode = yPlane.read(uint2(x, y)).r;
    const uint chromaX = p.flags.z == 0 ? min(x / 2, chromaWidth - 1)
                                        : min(x, chromaWidth - 1);
    const float uCode = float(uPlane.read(uint2(chromaX, y)).r);
    const float vCode = float(vPlane.read(uint2(chromaX, y)).r);

    float kr = 0.2126;
    float kb = 0.0722;
    if (p.flags.x == 5 || p.flags.x == 6) {
        kr = 0.299;
        kb = 0.114;
    } else if (p.flags.x == 9) {
        kr = 0.2627;
        kb = 0.0593;
    }
    const float scale = exp2(float(bitDepth) - 8.0);
    const bool fullRange = p.flags.y == 1;
    const float yLow = fullRange ? 0.0 : 16.0 * scale;
    const float yHigh = fullRange ? exp2(float(bitDepth)) - 1.0 : 235.0 * scale;
    const float cLow = fullRange ? 0.0 : 16.0 * scale;
    const float cHigh = fullRange ? exp2(float(bitDepth)) - 1.0 : 240.0 * scale;
    const float mid = (cLow + cHigh) * 0.5;
    const float yn = (float(yCode) - yLow) / (yHigh - yLow);
    const float uu = (uCode - mid) / (cHigh - cLow);
    const float vv = (vCode - mid) / (cHigh - cLow);
    const float dd = 1.0 - kr - kb;
    float r = yn + 2.0 * (1.0 - kr) * vv;
    float b = yn + 2.0 * (1.0 - kb) * uu;
    float g = (yn - kr * r - kb * b) / dd;
    if ((p.flags.w & 1) != 0 && (p.flags.w & 2) == 0) {
        const float alpha = float(aPlane.read(uint2(x, y)).r) / 65535.0;
        r *= alpha;
        g *= alpha;
        b *= alpha;
    }
    // P5.1：显示色彩管理——线性化 → 源/显示 primaries 级联矩阵 →
    // 输出线性显示 RGB（layer 的 tagged colorspace 由合成器编码）。
    if (p.colorConvert.x > 0.5) {
        float3 rgb = clamp(float3(r, g, b), 0.0, 1.0);
        const int eotf = int(p.colorConvert.y + 0.5);
        float3 linear;
        if (eotf == 2) {
            linear = rgb;
        } else if (eotf == 1) {
            // sRGB 逆 EOTF
            linear = select(rgb / 12.92, pow((rgb + 0.055) / 1.055, float3(2.4)),
                            rgb > 0.04045);
        } else {
            // ITU-R BT.709 逆 OETF（601/2020 同形近似）
            const float3 low = rgb / 4.5;
            const float3 high = pow((rgb + 0.099) / 1.099, float3(1.0 / 0.45));
            linear = select(low, high, rgb > 0.081);
        }
        const float3x3 m = float3x3(p.cmsRow0.xyz, p.cmsRow1.xyz, p.cmsRow2.xyz);
        const float3 display = m * linear;
        return float4(clamp(display, 0.0, 16.0), 1.0);
    }
    return float4(clamp(float3(r, g, b), 0.0, 1.0), 1.0);
}
"""#

    private let commandQueue: MTLCommandQueue?
    private let pipeline: MTLRenderPipelineState?
    private var currentFrame: ToposMovie.GPUFrame?
    private var aspectMode: AspectMode = .follow
    private var rotationDegrees = 0
    private var flipHorizontal = false
    private var flipVertical = false
    var onGPUConversionTime: ((Double) -> Void)?
    /// P5.1：当前显示色彩 profile（nil/identity = 关闭，走原 sRGB 直通）。
    private var colorProfile: DisplayColorProfile?
    /// 源 EOTF：0=BT.709 视频 1=sRGB 2=线性（float 源）。
    var sourceEOTF: Int = 0

    /// P5.1/P5.2：应用显示 profile + layer colorspace/EDR 配置。
    func applyColorProfile(_ profile: DisplayColorProfile?,
                           colorSpace: NSColorSpace?,
                           extendedRange: Bool) {
        colorProfile = profile
        if let layer = layer as? CAMetalLayer {
            layer.colorspace = colorSpace?.cgColorSpace
            if extendedRange {
                layer.pixelFormat = .rgba16Float
                layer.wantsExtendedDynamicRangeContent = true
                colorPixelFormat = .rgba16Float
            } else {
                layer.pixelFormat = .bgra8Unorm
                layer.wantsExtendedDynamicRangeContent = false
                colorPixelFormat = .bgra8Unorm
            }
            framebufferOnly = true
        }
        needsDisplay = true
    }

    /// P5.2 spike：当前屏 EDR headroom（>1 表示有 HDR 余量）。
    var edrHeadroom: Double {
        Double(window?.screen?.maximumExtendedDynamicRangeColorComponentValue ?? 1.0)
    }

    init(frame frameRect: NSRect, device: MTLDevice) {
        commandQueue = device.makeCommandQueue()
        if let library = playbackDiagnosticMakeLibrary(device, Self.shaderSource),
           let vertex = library.makeFunction(name: "topoplayer_vertex"),
           let fragment = library.makeFunction(name: "topoplayer_fragment") {
            let descriptor = MTLRenderPipelineDescriptor()
            descriptor.vertexFunction = vertex
            descriptor.fragmentFunction = fragment
            descriptor.colorAttachments[0].pixelFormat = .bgra8Unorm
            pipeline = try? device.makeRenderPipelineState(descriptor: descriptor)
        } else {
            pipeline = nil
        }
        super.init(frame: frameRect, device: device)
        delegate = self
        colorPixelFormat = .bgra8Unorm
        framebufferOnly = true
        enableSetNeedsDisplay = true
        isPaused = true
        clearColor = MTLClearColorMake(0, 0, 0, 1)
        autoresizingMask = [.width, .height]
    }

    required init(coder: NSCoder) { fatalError("init(coder:) is unsupported") }

    var isReady: Bool { commandQueue != nil && pipeline != nil }

    func display(_ frame: ToposMovie.GPUFrame, aspectMode: AspectMode,
                 rotationDegrees: Int, flipHorizontal: Bool, flipVertical: Bool) {
        currentFrame = frame
        self.aspectMode = aspectMode
        self.rotationDegrees = ((rotationDegrees % 360) + 360) % 360
        self.flipHorizontal = flipHorizontal
        self.flipVertical = flipVertical
        needsDisplay = true
    }

    func setAspectMode(_ mode: AspectMode) {
        aspectMode = mode
        needsDisplay = true
    }

    func setTransform(rotationDegrees: Int, flipHorizontal: Bool,
                      flipVertical: Bool) {
        self.rotationDegrees = ((rotationDegrees % 360) + 360) % 360
        self.flipHorizontal = flipHorizontal
        self.flipVertical = flipVertical
        needsDisplay = true
    }

    func draw(in view: MTKView) {
        guard let frame = currentFrame,
              frame.textures.count >= 3,
              let pipeline,
              let pass = currentRenderPassDescriptor,
              let drawable = currentDrawable,
              let commandBuffer = commandQueue?.makeCommandBuffer(),
              let encoder = commandBuffer.makeRenderCommandEncoder(descriptor: pass) else {
            return
        }
        encoder.setRenderPipelineState(pipeline)
        let cms = colorProfile
        let cmsEnabled = cms?.isEnabled ?? false
        let rows = cms?.packedRows() ?? [1, 0, 0, 0, 1, 0, 0, 0, 1]
        var uniforms = Uniforms(
            dimensions: SIMD4(UInt32(frame.width), UInt32(frame.height),
                              UInt32(frame.bitDepth), UInt32(frame.textures[1].width)),
            flags: SIMD4(UInt32(frame.colorMatrix), UInt32(frame.colorRange),
                         UInt32(frame.pixelFormat),
                         (frame.hasAlpha ? 1 : 0) | (frame.premultiplied ? 2 : 0)),
            aspect: SIMD4(Float(max(view.drawableSize.width, 1)
                                 / max(view.drawableSize.height, 1)),
                          sourceAspect(frame),
                          aspectMode == .stretch ? 1 : (aspectMode == .cropFill ? 2 : 0),
                          Float((rotationDegrees / 90) & 3)
                            + (flipHorizontal ? 4 : 0)
                            + (flipVertical ? 8 : 0)),
            colorConvert: SIMD4(cmsEnabled ? 1 : 0, Float(sourceEOTF), 0, 0),
            cmsRow0: SIMD4(rows[0], rows[1], rows[2], 0),
            cmsRow1: SIMD4(rows[3], rows[4], rows[5], 0),
            cmsRow2: SIMD4(rows[6], rows[7], rows[8], 0))
        encoder.setFragmentBytes(&uniforms, length: MemoryLayout<Uniforms>.stride,
                                 index: 0)
        // P2-07：池化纹理集在本命令缓冲的采样期间持有 GPU 引用；completed
        // 后释放——归零且 CPU 侧已逐出时纹理集才归还池中复用。
        frame.pooledTextures?.retainGPU()
        for index in 0..<min(frame.textures.count, 4) {
            encoder.setFragmentTexture(frame.textures[index], index: index)
        }
        encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 6)
        encoder.endEncoding()
        commandBuffer.present(drawable)
        commandBuffer.addCompletedHandler { [weak self] buffer in
            frame.pooledTextures?.releaseGPU()
            guard buffer.status == .completed,
                  buffer.gpuEndTime > buffer.gpuStartTime else { return }
            let milliseconds = (buffer.gpuEndTime - buffer.gpuStartTime) * 1000
            DispatchQueue.main.async { self?.onGPUConversionTime?(milliseconds) }
        }
        commandBuffer.commit()
        playbackDiagnosticDidDraw(frame.width, frame.height)
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {
        needsDisplay = true
    }

    private func sourceAspect(_ frame: ToposMovie.GPUFrame) -> Float {
        let width = Float(max(frame.width, 1))
        let height = Float(max(frame.height, 1))
        return rotationDegrees % 180 == 0 ? width / height : height / width
    }
}
