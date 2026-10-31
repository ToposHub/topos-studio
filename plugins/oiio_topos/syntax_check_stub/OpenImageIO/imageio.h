/* OpenImageIO 最小 stub —— 仅用于 topos_imageio.cpp 的 -fsyntax-only 检查
 * （本机无 OIIO SDK；接口面按 OIIO 2.x 公共 API 手写镜像，覆盖插件用到的
 * 全部符号。不得链接，仅类型检查。） */
#ifndef OIIO_STUB_IMAGEIO_H
#define OIIO_STUB_IMAGEIO_H

#include <cstdio>
#include <cstdarg>
#include <cstdint>
#include <string>
#include <vector>

#define OIIO_VERSION_STRING "2.5.0-stub"
#define OIIO_PLUGIN_VERSION 1

namespace OIIO {

using stride_t = int64_t;

class TypeDesc {
public:
    enum BASE { UINT16 };
    TypeDesc() = default;
    TypeDesc(BASE) {}
    bool operator==(const TypeDesc&) const { return true; }
    bool operator!=(const TypeDesc& o) const { return !(*this == o); }
};

class ImageSpec {
public:
    int width = 0, height = 0, nchannels = 0, depth = 1;
    TypeDesc format;
    std::vector<std::string> channelnames;
    ImageSpec() = default;
    ImageSpec(int w, int h, int nc, TypeDesc fmt)
        : width(w), height(h), nchannels(nc), format(fmt) {}
    void attribute(const std::string&, int) {}
    void attribute(const std::string&, float) {}
    void attribute(const std::string&, const char*) {}
    void attribute(const std::string&, const std::string&) {}
};

namespace Filesystem {
class IOProxy {};
inline FILE* fopen(const std::string&, const std::string&) { return NULL; }
inline void remove(const std::string&) {}
}  // namespace Filesystem

inline bool convert_types(const TypeDesc&, const void*, const TypeDesc&, void*,
                          size_t, stride_t, stride_t)
{
    return true;
}

class ImageInput {
public:
    virtual ~ImageInput() = default;
    virtual const char* format_name(void) const = 0;
    virtual bool valid_file(const std::string&, Filesystem::IOProxy*) const = 0;
    virtual bool open(const std::string&, ImageSpec&, const ImageSpec&) = 0;
    virtual bool close(void) = 0;
    virtual int current_subimage(void) const = 0;
    virtual bool seek_subimage(int, int) = 0;
    virtual bool read_native_scanline(int, int, int, int, void*) = 0;

protected:
    void errorf(const char* fmt, ...) { (void)fmt; }
};

class ImageOutput {
public:
    enum OpenMode { Create };
    virtual ~ImageOutput() = default;
    virtual const char* format_name(void) const = 0;
    virtual bool supports(const std::string&) const = 0;
    virtual bool open(const std::string&, const ImageSpec&, OpenMode) = 0;
    virtual bool close(void) = 0;
    virtual bool write_scanline(int, int, TypeDesc, const void*, stride_t) = 0;

protected:
    void errorf(const char* fmt, ...) { (void)fmt; }
};

}  // namespace OIIO

#define OIIO_PLUGIN_NAMESPACE_BEGIN namespace toos_stub {
#define OIIO_PLUGIN_NAMESPACE_END }
#define OIIO_PLUGIN_EXPORTS_BEGIN
#define OIIO_PLUGIN_EXPORTS_END
#define OIIO_EXPORT

#endif  // OIIO_STUB_IMAGEIO_H
