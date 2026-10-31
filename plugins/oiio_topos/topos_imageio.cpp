// topos_imageio.cpp —— OpenImageIO ImageInput/ImageOutput 插件（Topos Image 阶段 8）。
//
// 重要边界（ADR-I001 D14 / R-I15 / 计划书阶段 8 验收）：
//  - 只有能够发现并加载本插件的 OIIO 宿主（oiiotool、以 OIIO 运行时链接自研
//    工具等）才能读写 .toos；**不等于** Blender/Maya/Nuke/Photoshop 自动获得
//    支持——各宿主须单独交付插件；
//  - OIIO 无 C ABI：本插件按构建时的 OIIO 头文件版本编译，ABI 不匹配由
//    宿主加载机制拒绝（插件以 OIIO 版本命名构建产物，逐版本分发）；
//  - v1 不支持 tile/mipmap：supports("tiles")=false、ROI 随机解码返回失败；
//  - 像素直传：GBR 10/12-bit → OIIO uint16（左移对齐到 16-bit 容器，
//    见 spec §6.1 位深语义；不做任何色彩空间猜测，CICP 元数据透传）。
//
// 构建（可选组件；未安装 OIIO SDK 时跳过）：
//   cmake -DTOPOS_BUILD_OIIO_PLUGIN=ON ...   （需 OpenImageIO 开发头文件）
//   产物：topos.imageio / topos.imageio.dylib（OIIO 搜索路径或 OIIO_LIBRARY_PATH）
//
// 测试：
//   oiiotool --info beauty.toos
//   oiiotool beauty.toos -o out.exr        # 往返按格式能力验收（见 README）
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include <OpenImageIO/imageio.h>

#include "topos_codec.h"
#include "topos_image.h"

OIIO_PLUGIN_NAMESPACE_BEGIN

using namespace OIIO;

namespace topos {

static std::string
last_detail(int32_t rc)
{
    const char* d = tc_last_error();
    return std::string(tc_image_status_message(rc)) +
           (d && *d ? " (" + std::string(d) + ")" : "");
}

// 简单文件读取器（probe/validate/decode 共用 topos_io 契约；读侧 open 与
// 写侧 §12 自检共享）
struct FileReader {
    FILE* f = NULL;
    topos_io io;
    explicit FileReader(const std::string& path)
    {
        f = fopen(path.c_str(), "rb");
        memset(&io, 0, sizeof(io));
        io.struct_size = (uint32_t)sizeof(io);
        io.abi_version = TOPOS_CODEC_ABI_VERSION;
        io.ctx = this;
        io.read = &FileReader::read_thunk;
        if (f != NULL) {
            fseek(f, 0, SEEK_END);
            io.length = (uint64_t)ftell(f);
        }
    }
    ~FileReader() { if (f) fclose(f); }
    FileReader(const FileReader&) = delete;
    FileReader& operator=(const FileReader&) = delete;
    static int32_t
    read_thunk(void* ctx, uint64_t off, void* buf, size_t n)
    {
        FileReader* s = (FileReader*)ctx;
        if (s->f == NULL || fseek(s->f, (long)off, SEEK_SET) != 0
            || fread(buf, 1, n, s->f) != n) {
            return TC_ERR_IO;
        }
        return TC_OK;
    }
};

// 把 planar uint16（GBR 序）打包成交错的 16-bit RGB(A)（OIIO 期望的内存布局）。
// 刻度契约（与 toos_tools/ADR-I009 一致）：codec 彩色平面 = 声明位深满刻度
// （0..2^bd-1），alpha 平面恒为 0..65535 容器 → OIIO uint16 全刻度：
// 彩色左移对齐（shift = 16-bd），alpha 恒等直通。
static void
pack_interleaved(const uint16_t* g, const uint16_t* b, const uint16_t* r,
                 const uint16_t* a, int nchans, size_t npixels, int shift,
                 uint16_t* out)
{
    for (size_t i = 0; i < npixels; ++i) {
        out[i * nchans + 0] = (uint16_t)((uint32_t)r[i] << shift);
        out[i * nchans + 1] = (uint16_t)((uint32_t)g[i] << shift);
        out[i * nchans + 2] = (uint16_t)((uint32_t)b[i] << shift);
        if (nchans > 3) {
            out[i * nchans + 3] = a[i];
        }
    }
}

class ToposImageInput final : public ImageInput {
public:
    ToposImageInput() = default;
    ~ToposImageInput() override { close(); }

    const char* format_name(void) const override { return "toos"; } /* OIIO 以 format_name 作默认扩展名：必须与文件扩展一致，不得用 "topos"（工程包扩展） */

    bool
    valid_file(const std::string& name, Filesystem::IOProxy* /*ioproxy*/) const override
    {
        FILE* f = Filesystem::fopen(name, "rb");
        if (f == NULL) {
            return false;
        }
        uint8_t head[4] = {0, 0, 0, 0};
        const size_t got = fread(head, 1, 4, f);
        fclose(f);
        return got == 4 && head[0] == 'T' && head[1] == 'P' && head[2] == 'I'
               && head[3] == 'M';
    }

    bool
    open(const std::string& name, ImageSpec& newspec,
         const ImageSpec& /*config*/) override
    {
        m_filename = name;
        m_io.reset(new FileReader(name));
        int32_t rc = tc_image_probe(&m_io->io, &m_info);
        if (rc != TC_OK) {
            errorf("topos: probe failed: %s", last_detail(rc).c_str());
            return false;
        }
        // pf=0（YUV 4:2:2 / Preview）色度平面半宽：本插件的整平面满分辨率
        // 打包对子采样平面不成立（会得到错位色度）。按"显式拒绝优于静默
        // 错误"原则拒读（与 toos_tools 同策略）；pf=1（YUV 4:4:4，满分辨率）
        // 与 pf=2（GBR）可读，通道名如实标注，不做色彩矩阵猜测。
        if (m_info.pixel_format == 0u) {
            errorf("topos: YUV 4:2:2 (Preview) unsupported by OIIO plugin v1 "
                   "(subsampled chroma planes); convert to GBR first");
            return false;
        }
        if (m_info.bit_depth != 10u && m_info.bit_depth != 12u) {
            errorf("topos: unsupported bit depth %u", (int)m_info.bit_depth);
            return false;
        }
        m_color_shift = 16 - (int)m_info.bit_depth;
        m_spec = ImageSpec((int)m_info.visible_width, (int)m_info.visible_height,
                           m_info.has_alpha ? 4 : 3, TypeDesc::UINT16);
        // 色彩元数据透传（不做 sRGB 猜测；unspecified 如实报告）
        m_spec.attribute("oiio:ColorSpace", "Linear");
        m_spec.attribute("topos:imageProfile",
                         m_info.idsc.image_profile == TC_IMG_PROFILE_PREVIEW
                             ? "Preview"
                             : (m_info.idsc.image_profile == TC_IMG_PROFILE_HQ
                                    ? "HQ"
                                    : "XQ"));
        m_spec.attribute("topos:ValidBitDepth", (int)m_info.bit_depth);
        // 两个独立键：AlphaMode = codec 枚举（int，恒有值）；
        // AlphaAssociation = straight/premultiplied（string，仅有 alpha 时）
        m_spec.attribute("topos:AlphaMode", (int)m_info.alpha_mode);
        m_spec.attribute("PixelAspectRatio",
                         (float)m_info.idsc.pixel_aspect_num
                             / (float)m_info.idsc.pixel_aspect_den);
        m_spec.attribute("orientation", (int)m_info.idsc.orientation);
        m_spec.channelnames.clear();
        if (m_info.pixel_format == 2) {
            m_spec.channelnames = { "G", "B", "R" };  // pf=2 平面序（C-130）
        } else {
            m_spec.channelnames = { "Y", "U", "V" };
        }
        if (m_info.has_alpha) {
            m_spec.channelnames.push_back("A");
            m_spec.attribute("topos:AlphaAssociation",
                             m_info.alpha_premultiplied ? "premultiplied"
                                                        : "straight");
        }
        newspec = m_spec;
        return true;
    }

    bool
    close(void) override
    {
        m_io.reset();
        return true;
    }

    int
    current_subimage(void) const override
    {
        return 0;
    }

    bool
    seek_subimage(int /*subimage*/, int /*miplevel*/) override
    {
        return true;  // 单图单 mip（v1 非目标：多图/mipmap）
    }

    bool
    read_native_scanline(int /*subimage*/, int /*miplevel*/, int y, int /*z*/,
                         void* data) override
    {
        if (y < 0 || y >= m_spec.height) {
            errorf("topos: scanline y=%d out of range", y);
            return false;
        }
        if (m_planes.empty() && !decode_all()) {
            return false;
        }
        const int w = m_spec.width;
        const int nch = m_spec.nchannels;
        const size_t row = (size_t)y * w;
        // 平面基址：m_planes[0] 是元素引用不是指针（旧代码 `m_planes[0] + row`
        // 实为整数运算，从未编译暴露）；平面 p 基址 = data() + p*w*h
        const size_t plane_sz = (size_t)m_spec.width * m_spec.height;
        uint16_t* base = m_planes.data();
        pack_interleaved(base + row, base + plane_sz + row,
                         base + 2 * plane_sz + row,
                         nch > 3 ? base + 3 * plane_sz + row : NULL, nch,
                         (size_t)w, m_color_shift, (uint16_t*)data);
        return true;
    }

private:
    bool
    decode_all(void)
    {
        // 一次性解码到平面缓冲（scanline 请求由此按行打包）
        const int nplanes = (int)m_info.plane_count;
        const size_t plane_sz = (size_t)m_info.visible_width
                              * (size_t)m_info.visible_height;
        m_planes.assign((size_t)nplanes * plane_sz, 0);
        std::vector<uint16_t*> ptrs(TC_FRAME_MAX_PLANES, NULL);
        for (int p = 0; p < nplanes; ++p) {
            // 旧代码 `ptrs[p] = data() + p * w * h` 用了从未赋值的局部
            // w=0/h=0 → 四个平面全指向缓冲开头互相覆写（从未编译暴露）
            ptrs[p] = m_planes.data() + (size_t)p * plane_sz;
        }
        topos_plane_view views[TC_FRAME_MAX_PLANES];
        memset(views, 0, sizeof(views));
        for (int p = 0; p < nplanes; ++p) {
            views[p].struct_size = (uint32_t)sizeof(topos_plane_view);
            views[p].abi_version = TOPOS_CODEC_ABI_VERSION; // plane_view 为 codec 域结构
            views[p].pixels = ptrs[p];
            views[p].stride = 0;
        }
        topos_frame_output out;
        memset(&out, 0, sizeof(out));
        out.struct_size = (uint32_t)sizeof(out);
        out.abi_version = TOPOS_CODEC_ABI_VERSION;
        const int32_t rc = tc_image_decode(&m_io->io, views, &out);
        if (rc != TC_OK) {
            errorf("topos: decode failed: %s", last_detail(rc).c_str());
            return false;
        }
        return true;
    }

    std::string m_filename;
    std::unique_ptr<FileReader> m_io;
    topos_image_info m_info;
    ImageSpec m_spec;
    std::vector<uint16_t> m_planes;
    int m_color_shift = 0;  // 彩色平面 10/12-bit → 16-bit 容器的左移量
};

class ToposImageOutput final : public ImageOutput {
public:
    const char* format_name(void) const override { return "toos"; } /* OIIO 以 format_name 作默认扩展名：必须与文件扩展一致，不得用 "topos"（工程包扩展） */

    bool
    supports(const std::string& feature) const override
    {
        // v1 能力：仅 scanline RGB(A)；tiles/mipmap/多图明确 capability=false
        return feature == "alpha" || feature == "nchannels";
    }

    bool
    open(const std::string& name, const ImageSpec& newspec,
         OpenMode mode = Create) override
    {
        if (mode != Create) {
            errorf("topos: MIP/multi-image output unsupported (v1)");
            return false;
        }
        if (newspec.nchannels < 3 || newspec.nchannels > 4) {
            errorf("topos: only RGB/RGBA output supported");
            return false;
        }
        if (newspec.format != TypeDesc::UINT16) {
            errorf("topos: request UINT16 scanlines (10/12-bit container)");
            return false;
        }
        m_filename = name;
        m_spec = newspec;
        m_pixels.assign((size_t)m_spec.width * m_spec.height * m_spec.nchannels, 0);
        m_scanlines = 0;
        return true;
    }

    bool
    write_scanline(int y, int /*z*/, TypeDesc format, const void* data,
                   stride_t xstride) override
    {
        // 只接受顺序整行写入（乱序/重复/越界显式拒绝——旧实现忽略 y，
        // 乱序 scanline 会被静默拼接成错位图像）
        if (y != m_scanlines || y < 0 || y >= m_spec.height) {
            errorf("topos: scanline y=%d out of order (expected %d of %d)",
                   y, m_scanlines, m_spec.height);
            return false;
        }
        const int nch = m_spec.nchannels;
        const int w = m_spec.width;
        std::vector<uint16_t> tmp(w * nch);
        if (!convert_types(format, data, TypeDesc::UINT16, tmp.data(),
                           w * nch, xstride, (stride_t)sizeof(uint16_t) * nch)) {
            errorf("topos: scanline conversion failed");
            return false;
        }
        memcpy(m_pixels.data() + (size_t)m_scanlines * w * nch, tmp.data(),
               (size_t)w * nch * sizeof(uint16_t));
        ++m_scanlines;
        return true;
    }

    bool
    close(void) override
    {
        if (m_filename.empty()) {
            return true;
        }
        if (m_scanlines != m_spec.height) {
            errorf("topos: truncated image (%d/%d scanlines)", (int)m_scanlines,
                   m_spec.height);
            m_filename.clear();
            return false;
        }
        // 交错 [R,G,B,(A)] → topos 平面序 G,B,R,(A)（ADR-C017 C-130）。
        // 刻度契约（codec 输入约定，与读侧/toos_tools 对称）：OIIO uint16
        // 全刻度（0..65535）→ 彩色四舍五入+饱和到 10-bit 满刻度（0..1023），
        // alpha 恒等直通（A16 容器 0..65535）。旧实现原样透传，高位全废。
        const int w = m_spec.width, h = m_spec.height;
        const int nch = m_spec.nchannels;
        const int bd = 10;
        const int shift = 16 - bd;
        const uint32_t half = 1u << (shift - 1);
        const uint32_t max_v = (1u << bd) - 1u;
        std::vector<uint16_t> planes(nch * (size_t)w * h);
        uint16_t* g = planes.data();
        uint16_t* b = g + (size_t)w * h;
        uint16_t* r = b + (size_t)w * h;
        uint16_t* a = nch > 3 ? r + (size_t)w * h : NULL;
        for (int y = 0; y < h; ++y) {
            for (int x = 0; x < w; ++x) {
                const size_t i = (size_t)y * w + x;
                const uint16_t* px = m_pixels.data() + i * nch;
                r[i] = (uint16_t)std::min(((uint32_t)px[0] + half) >> shift, max_v);
                g[i] = (uint16_t)std::min(((uint32_t)px[1] + half) >> shift, max_v);
                b[i] = (uint16_t)std::min(((uint32_t)px[2] + half) >> shift, max_v);
                if (a) a[i] = px[3];
            }
        }
        topos_frame_config cfg;
        memset(&cfg, 0, sizeof(cfg));
        cfg.struct_size = (uint32_t)sizeof(cfg);
        cfg.abi_version = TOPOS_CODEC_ABI_VERSION;
        cfg.visible_width = (uint16_t)w;
        cfg.visible_height = (uint16_t)h;
        cfg.profile = 5;
        cfg.pixel_format = 2;
        cfg.bit_depth = (uint8_t)bd;
        cfg.qp_base = 28;
        cfg.color_range = 1;
        cfg.color_primaries = 1;
        cfg.color_transfer = 1;
        cfg.color_matrix = 0;  // GBR identity
        if (nch > 3) {
            cfg.alpha_mode = 1;
            cfg.alpha_bit_depth = 16;
        }
        topos_frame_input input;
        memset(&input, 0, sizeof(input));
        input.struct_size = (uint32_t)sizeof(input);
        input.abi_version = TOPOS_CODEC_ABI_VERSION;
        input.planes[0] = g;
        input.planes[1] = b;
        input.planes[2] = r;
        input.planes[3] = a;
        std::vector<uint8_t> packet(tc_frame_packet_bound(&cfg));
        topos_frame_stats stats;
        memset(&stats, 0, sizeof(stats));
        stats.struct_size = (uint32_t)sizeof(stats);
        stats.abi_version = TOPOS_CODEC_ABI_VERSION;
        const int32_t erc = tc_frame_encode(&cfg, &input, packet.data(),
                                            packet.size(), &stats);
        if (erc != TC_OK) {
            errorf("topos: encode failed: %s", tc_status_message(erc));
            m_filename.clear();
            return false;
        }
        topos_image_idsc idsc;
        if (tc_image_derive_idsc(packet.data(), stats.packet_size,
                                 TC_IMG_PROFILE_HQ, &idsc) != TC_OK) {
            errorf("topos: idsc derive failed");
            m_filename.clear();
            return false;
        }
        topos_image_write_params wp;
        memset(&wp, 0, sizeof(wp));
        wp.struct_size = (uint32_t)sizeof(wp);
        wp.abi_version = TOPOS_IMAGE_ABI_VERSION;
        wp.idsc = idsc;
        wp.pixl_data = packet.data();
        wp.pixl_size = stats.packet_size;
        // 原子写：临时文件 + 自检 + rename（spec §12）
        const std::string tmp = m_filename + ".tmp-oiio";
        FILE* f = fopen(tmp.c_str(), "wb");
        if (f == NULL) {
            errorf("topos: cannot create %s", tmp.c_str());
            m_filename.clear();
            return false;
        }
        FileSink sink(f);
        topos_io io;
        memset(&io, 0, sizeof(io));
        io.struct_size = (uint32_t)sizeof(io);
        io.abi_version = TOPOS_CODEC_ABI_VERSION;
        io.ctx = &sink;
        io.write = &FileSink::write_thunk;
        io.seek_write = &FileSink::seek_thunk;
        uint64_t written = 0;
        const int32_t wrc = tc_image_write(&wp, &io, &written);
        fflush(f);
        fclose(f);
        if (wrc != TC_OK) {
            errorf("topos: write failed: %s", last_detail(wrc).c_str());
            Filesystem::remove(tmp);
            m_filename.clear();
            return false;
        }
        // spec §12 步骤 4：rename 前对临时文件完整 validate（写后自检；
        // 与 toos CLI 原子写流程一致，杜绝半文件冒充成功）
        {
            FileReader vf(tmp);
            topos_image_info vinfo;
            memset(&vinfo, 0, sizeof(vinfo));
            if (vf.f == NULL
                || tc_image_validate(&vf.io, 0, &vinfo) != TC_OK) {
                errorf("topos: write-back self check failed");
                Filesystem::remove(tmp);
                m_filename.clear();
                return false;
            }
        }
        if (rename(tmp.c_str(), m_filename.c_str()) != 0) {
            errorf("topos: rename failed");
            Filesystem::remove(tmp);
            m_filename.clear();
            return false;
        }
        m_filename.clear();
        return true;
    }

private:
    struct FileSink {
        FILE* f;
        int failed = 0;
        explicit FileSink(FILE* fh) : f(fh) {}
        static int32_t
        write_thunk(void* ctx, const void* d, size_t n)
        {
            FileSink* s = (FileSink*)ctx;
            if (fwrite(d, 1, n, s->f) != n) { s->failed = 1; return TC_ERR_IO; }
            return TC_OK;
        }
        static int32_t
        seek_thunk(void* ctx, uint64_t off, const void* d, size_t n)
        {
            FileSink* s = (FileSink*)ctx;
            if (fseek(s->f, (long)off, SEEK_SET) != 0
                || fwrite(d, 1, n, s->f) != n) { s->failed = 1; return TC_ERR_IO; }
            return TC_OK;
        }
    };

    std::string m_filename;
    ImageSpec m_spec;
    std::vector<uint16_t> m_pixels;
    int m_scanlines = 0;
};

}  // namespace topos

OIIO_PLUGIN_EXPORTS_BEGIN

OIIO_EXPORT ImageInput*
topos_input_imageio_create()
{
    return new topos::ToposImageInput();
}
OIIO_EXPORT const char*
topos_input_imageio_version()
{
    return OIIO_VERSION_STRING;
}
OIIO_EXPORT int
topos_imageio_version_cb(void)
{
    return OIIO_PLUGIN_VERSION;
}
OIIO_EXPORT ImageOutput*
topos_output_imageio_create()
{
    return new topos::ToposImageOutput();
}
OIIO_EXPORT const char*
topos_output_imageio_version()
{
    return OIIO_VERSION_STRING;
}

OIIO_PLUGIN_EXPORTS_END

OIIO_PLUGIN_NAMESPACE_END
