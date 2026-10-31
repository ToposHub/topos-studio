"""B1.2 CUDA→GL 互操作与路线选择 PoC（计划表 B · Windows 线）。

目的（计划 §B1.2）：对 B1.1 唯一可行候选（cuda 驻留帧）完成真实渲染消费
验证，并与现生产两条路径做同机成对比较，输出路线选择证据：

1. **capability**：GL/WGL/CUDA-GL interop 能力实录（WGL 扩展、设备匹配、
   GL_TEXTURE_2D_ARRAY 注册探测）；
2. **matrix**：NV12（R8+RG8）与 P010（R16UI+RG16UI，生产 usampler 语义）
   的 cuda→GL 注册/映射/DtoA 拷贝正确性——纹理 readback 逐字节 md5 对拍
   同解码器下载参考 + **FBO 真采样**对拍（计划："可注册纹理"不等于两平面
   可被正确采样）；
3. **timing**：一致终点「GL 纹理就绪可采样」的三路径同机成对计时
   （≥5 组交错采样）：cpu_planar（软解+行提取+tex.write）/
   hw_download_planar（硬解+下载+行提取+tex.write）/
   hw_zero_copy（硬解驻留+DLPack+cuMemcpy2D，逐帧与解码会话交织）；
4. **limits**：注册资源数上限（≥256 纹理）+ 全量 map/unmap + 恢复性；
5. **failures**：可控失效注入——进程内安全项每项后恢复循环；危险项
   （注册期删 GL 纹理/GL 上下文销毁/无上下文 map/垃圾句柄/-129 毒化矩阵）
   在**子进程**执行，崩溃即证据。

**本机 ABI/时序实录（驱动 621.01 / CUDA 13.5 / RTX 4090，2026-10-24）**：

- B1.1 的"纯 ctypes 可行"需修正：ctypes 路径在本机对 cuMemcpy2D 不成立
  ——未版本化 `cuMemcpy2D` 返回 rc=201（与 B1.1 cuMemAlloc 同病），
  `cuMemcpy2D_v2` 以同参数值返回 rc=1（结构体 ABI 不符），而**相同参数
  经 cuda-bindings（cuda-python 13.4.2，本 venv 已随 torch 栈安装）全部
  成功**。故本 PoC 与后续生产接线统一使用 cuda-bindings。
- **-129 毒化机制（B1.1 结论修正版）**：PyAV `is_hw_owned=True`
  （primary_ctx=1）会话在主上下文被本进程初始化过后**永久** -129
  （unbind 无效，`cuDevicePrimaryCtxReset` 可恢复但破坏活跃会话）；
  `is_hw_owned=False` 会话（FFmpeg 私有 ctx 路径）完全免疫。因此 interop
  一律使用**私有上下文**（cuCtxCreate），主上下文永不触碰。
- 驱动对 interop 句柄**不做合法性校验**（垃圾句柄 map 直接访问违例），
  句柄合法性必须由我方代码负责。

计时归因（三路径一致）：decode 段 = next(迭代器)（软解或硬解+隐式下载），
xfer 段 = 行提取/DLPack 解析 + map/copy/unmap 或 tex.write。

fail-closed：阶段失败记录结构化 reason 后继续；退出码 0=全部阶段完成，
2=有阶段异常。真机设备移除（TDR/拔卡）无法安全注入，按计划「先可控故障
注入」如实登记为后续人工项，不用删除设备 API 冒充。

依赖：cuda-bindings（`from cuda.bindings import driver`）。

用法：
    .venv/Scripts/python.exe scripts/poc_cuda_gl_interop.py --tag B12_20261024_run01
    .venv/Scripts/python.exe scripts/poc_cuda_gl_interop.py --fault-child delete_tex_while_registered
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import av  # noqa: E402
import moderngl  # noqa: E402
import numpy as np  # noqa: E402
from av.codec.hwaccel import HWAccel, HWDeviceType  # noqa: E402
from cuda.bindings import driver as cu  # noqa: E402

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"

GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE_2D_ARRAY = 0x8C1A

RESULT: dict = {
    "schema": "poc_cuda_gl_interop/2",
    "date": datetime.now().isoformat(timespec="seconds"),
    "pyav": av.__version__,
    "phases": [],
}
ISSUES: list[str] = []


def phase(name: str, fn) -> dict:
    entry: dict = {"phase": name, "status": "ok"}
    try:
        entry.update(fn() or {})
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"
        ISSUES.append(f"{name}: {exc}")
        if "--debug" in sys.argv:
            traceback.print_exc()
    RESULT["phases"].append(entry)
    brief = {k: v for k, v in entry.items() if k not in ("phase", "status")}
    print(f"[{entry['status']:>5}] {name}: "
          f"{json.dumps(brief, ensure_ascii=False)[:220]}")
    return entry


# ---------------------------------------------------------------------------
# DLPack 胶囊解析（B1.1 实证偏移，与本机 PyAV 18.1.0 布局对拍过）
# ---------------------------------------------------------------------------

def _parse_dlpack_capsule(capsule) -> dict:
    pycapsule = ctypes.pythonapi
    pycapsule.PyCapsule_GetName.restype = ctypes.c_char_p
    pycapsule.PyCapsule_GetName.argtypes = [ctypes.py_object]
    pycapsule.PyCapsule_GetPointer.restype = ctypes.c_void_p
    pycapsule.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    name = pycapsule.PyCapsule_GetName(capsule)
    base = pycapsule.PyCapsule_GetPointer(capsule, name)

    device_type = ctypes.c_int32.from_address(base + 8).value
    device_id = ctypes.c_int32.from_address(base + 12).value
    ndim = ctypes.c_int32.from_address(base + 16).value
    dtype_bits = ctypes.c_uint8.from_address(base + 21).value
    itemsize = max(1, dtype_bits // 8)
    shape_ptr = ctypes.c_void_p.from_address(base + 24).value or 0
    strides_ptr = ctypes.c_void_p.from_address(base + 32).value
    byte_offset = ctypes.c_uint64.from_address(base + 40).value
    data = ctypes.c_void_p.from_address(base + 0).value
    shape = [ctypes.c_int64.from_address(shape_ptr + 8 * i).value
             for i in range(ndim)]
    strides = ([ctypes.c_int64.from_address(strides_ptr + 8 * i).value
                for i in range(ndim)] if strides_ptr else None)
    return {
        "device_type": device_type, "device_id": device_id,
        "ndim": ndim, "itemsize": itemsize,
        "shape": shape, "strides": strides,
        "data_ptr": data, "byte_offset": byte_offset,
    }


def _plane_geometry(info: dict, row_bytes: int) -> dict:
    """DLPack 信息 → 拷贝几何（pitch 元素单位 → 字节，见 B1.1 报告）。"""
    if info["strides"]:
        pitch = info["strides"][0] * info["itemsize"]
    else:
        pitch = row_bytes
    return {
        "device_ptr": info["data_ptr"] + info["byte_offset"],
        "pitch": pitch,
        "row_bytes": row_bytes,
    }


# ---------------------------------------------------------------------------
# CUDA-GL interop（cuda-bindings；私有上下文，见模块 docstring 实录）
# ---------------------------------------------------------------------------

class CudaGL:
    """cuda.bindings.driver 的 interop 薄封装。

    上下文策略：interop 一律私有上下文（cuCtxCreate），从不触碰主上下文
    （-129 毒化机制见模块 docstring 与 ctx_poison_matrix 探针）。
    """

    def __init__(self, use_private_ctx: bool = True) -> None:
        err, = cu.cuInit(0)
        if int(err) != 0:
            raise RuntimeError(f"cuInit: {err}")
        err, ver = cu.cuDriverGetVersion()
        self.driver_version = int(ver)
        err, name = cu.cuDeviceGetName(128, cu.CUdevice(0))
        if isinstance(name, (bytes, bytearray)):
            name = bytes(name).split(b"\x00", 1)[0].decode("utf-8", "replace")
        self.device_name = str(name)
        if use_private_ctx:
            err, self.ctx = cu.cuCtxCreate(None, 0, cu.CUdevice(0))
        else:
            err, self.ctx = cu.cuDevicePrimaryCtxRetain(cu.CUdevice(0))
        if int(err) != 0:
            raise RuntimeError(f"ctx create/retain: {err}")
        err, = cu.cuCtxSetCurrent(self.ctx)
        if int(err) != 0:
            raise RuntimeError(f"cuCtxSetCurrent: {err}")
        self.ctx_kind = "private" if use_private_ctx else "primary"

    @staticmethod
    def err_str(rc) -> str:
        try:
            e, s = cu.cuGetErrorString(rc)
            if str(e) != "CUDA_SUCCESS":
                return str(rc)
            if isinstance(s, (bytes, bytearray)):
                return bytes(s).split(b"\x00", 1)[0].decode(
                    "utf-8", "replace")
            return str(s)
        except Exception:
            return str(rc)

    def register_image(self, texture, target: int, flags: int = 0):
        err, res = cu.cuGraphicsGLRegisterImage(
            texture.glo, target, flags)
        if int(err) != 0:
            raise RuntimeError(
                f"cuGraphicsGLRegisterImage {int(err)} ({self.err_str(err)})")
        return res

    @staticmethod
    def unregister(res) -> int:
        err, = cu.cuGraphicsUnregisterResource(res)
        return int(err)

    @staticmethod
    def map_one(res) -> int:
        err, = cu.cuGraphicsMapResources(1, res, 0)
        return int(err)

    @staticmethod
    def unmap_one(res) -> int:
        err, = cu.cuGraphicsUnmapResources(1, res, 0)
        return int(err)

    @staticmethod
    def mapped_array(res, array_index: int = 0, mip: int = 0):
        return cu.cuGraphicsSubResourceGetMappedArray(
            res, array_index, mip)

    def copy_to_texture(
        self, res, device_ptr: int, pitch: int,
        row_bytes: int, rows: int,
    ) -> None:
        """map → 取 CUDA array → DtoA 2D 拷贝 → unmap（GL/CUDA 同步由
        map/unmap 提供）。"""
        rc = self.map_one(res)
        if rc != 0:
            raise RuntimeError(f"map {rc} ({self.err_str(rc)})")
        try:
            err, arr = self.mapped_array(res)
            if int(err) != 0:
                raise RuntimeError(
                    f"getMappedArray {int(err)} ({self.err_str(err)})")
            m = cu.CUDA_MEMCPY2D()
            m.srcMemoryType = cu.CUmemorytype.CU_MEMORYTYPE_DEVICE
            m.srcDevice = device_ptr
            m.srcPitch = pitch
            m.dstMemoryType = cu.CUmemorytype.CU_MEMORYTYPE_ARRAY
            m.dstArray = arr
            m.WidthInBytes = row_bytes
            m.Height = rows
            err, = cu.cuMemcpy2D(m)
            if int(err) != 0:
                raise RuntimeError(
                    f"cuMemcpy2D {int(err)} ({self.err_str(err)})")
        finally:
            self.unmap_one(res)


# ---------------------------------------------------------------------------
# 平面工具
# ---------------------------------------------------------------------------

def _plane_mv(plane) -> memoryview | bytes:
    """生产语义的平面访问：优先 memoryview（行切片免整面预拷贝，
    对应 _extract_visible_plane_data），不支持时回退 bytes。"""
    try:
        return memoryview(plane)
    except TypeError:
        return bytes(plane)


def _rows(buf, pitch: int, row_bytes: int, rows: int) -> bytes:
    if pitch == row_bytes:
        return bytes(buf[:row_bytes * rows])
    return b"".join(
        bytes(buf[r * pitch:r * pitch + row_bytes]) for r in range(rows))


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _plane_layout(fmt: str, width: int, height: int) -> dict:
    """契约平面几何：NV12 → (Y R8, UV RG8)；P010 → (Y R16UI, UV RG16UI)。
    与生产 planar 上传（yuv_gpu_upload.py R8/R16UI 池）同构。"""
    if fmt == "nv12":
        return {
            "planes": [
                {"component": "Y", "tex": ("f1", 1), "w": width, "h": height,
                 "row_bytes": width, "rows": height},
                {"component": "UV", "tex": ("f1", 2), "w": width // 2,
                 "h": height // 2, "row_bytes": width, "rows": height // 2},
            ],
            "bit_depth": 8,
        }
    if fmt in ("p010le", "p016le"):
        return {
            "planes": [
                {"component": "Y", "tex": ("u2", 1), "w": width, "h": height,
                 "row_bytes": width * 2, "rows": height},
                {"component": "UV", "tex": ("u2", 2), "w": width // 2,
                 "h": height // 2, "row_bytes": width * 2,
                 "rows": height // 2},
            ],
            "bit_depth": 10 if fmt == "p010le" else 16,
        }
    raise ValueError(f"unsupported hw format {fmt}")


def _make_textures(ctx: moderngl.Context, layout: dict) -> list:
    return [
        ctx.texture((p["w"], p["h"]), components=p["tex"][1], dtype=p["tex"][0])
        for p in layout["planes"]
    ]


# ---------------------------------------------------------------------------
# 采样验证 shader（NV12 归一化采样 / P010 usampler 整数采样两条）
# ---------------------------------------------------------------------------

_VERT = """
#version 330
in vec2 in_pos;
void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

_FRAG_NORM = """
#version 330
uniform sampler2D y_tex;
uniform sampler2D uv_tex;
out vec4 out_color;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    float y = texelFetch(y_tex, p, 0).r;
    vec2 uvg = texelFetch(uv_tex, ivec2(p.x / 2, p.y / 2), 0).rg;
    out_color = vec4(y, uvg.r, uvg.g, 1.0);
}
"""

_FRAG_UINT = """
#version 330
uniform usampler2D y_tex;
uniform usampler2D uv_tex;
out vec4 out_color;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    float y = float(texelFetch(y_tex, p, 0).r) / 65535.0;
    vec2 uvg = vec2(texelFetch(uv_tex, ivec2(p.x / 2, p.y / 2), 0).rg)
        / vec2(65535.0);
    out_color = vec4(y, uvg.r, uvg.g, 1.0);
}
"""


def _sampled_fbo_check(
    ctx: moderngl.Context, textures: list, ref_y: bytes, ref_uv: bytes,
    width: int, height: int, bit_depth: int,
) -> dict:
    """FBO 真采样对拍：GPU 采样两平面 → RGBA8，与 CPU 期望（同公式）比对。
    readback 逐字节 md5 已另行完成，此处允许 ≤1 LSB 舍入差。"""
    frag = _FRAG_UINT if bit_depth > 8 else _FRAG_NORM
    prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
    quad = np.array(
        [-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1], dtype="f4")
    vbo = ctx.buffer(quad.tobytes())
    vao = ctx.vertex_array(prog, vbo, "in_pos")
    out_tex = ctx.texture((width, height), components=4, dtype="f1")
    fbo = ctx.framebuffer(color_attachments=[out_tex])

    textures[0].use(0)
    textures[1].use(1)
    prog["y_tex"].value = 0
    prog["uv_tex"].value = 1
    fbo.use()
    ctx.clear(0.0, 0.0, 0.0, 1.0)
    vao.render()

    # glReadPixels 行序自底向上，行 0 = gl_FragCoord.y=0.5 = 屏幕底行；
    # 屏幕底行 texelFetch 纹理行 0（拷贝自源顶行）→ read 行 0 恰为源顶行，
    # 与参考（自顶向下）天然对齐，无需翻转。
    raw = fbo.read(components=4)
    got = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)

    dtype = np.uint16 if bit_depth > 8 else np.uint8
    y = np.frombuffer(ref_y, dtype=dtype).reshape(height, width)
    # uv 紧凑缓冲每行 width 个样本（交错 U/V），NV12 字节、P010 uint16
    uv = np.frombuffer(ref_uv, dtype=dtype).reshape(height // 2, width)
    max_v = 65535.0 if bit_depth > 8 else 255.0
    y_n = y.astype(np.float32) / max_v
    u = uv[:, 0::2].astype(np.float32) / max_v
    v = uv[:, 1::2].astype(np.float32) / max_v
    u_up = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)
    v_up = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)
    expected = np.stack(
        [y_n, u_up, v_up, np.ones_like(y_n)], axis=-1).astype(np.float32)
    expected_u8 = np.clip(np.round(expected * 255.0), 0, 255).astype(np.uint8)

    diff = np.abs(got.astype(np.int16) - expected_u8.astype(np.int16))
    vao.release()
    vbo.release()
    fbo.release()
    out_tex.release()
    prog.release()
    return {
        "max_abs_diff": int(diff.max()),
        "mean_abs_diff": float(diff.mean()),
        "pass": bool(diff.max() <= 1),
    }


# ---------------------------------------------------------------------------
# 阶段实现
# ---------------------------------------------------------------------------

def run_capability() -> dict:
    ctx = moderngl.create_standalone_context()
    info = ctx.info
    wgl_ext = ""
    try:
        gl = ctypes.CDLL("opengl32.dll")
        gl.wglGetCurrentContext.restype = ctypes.c_void_p
        gl.wglGetProcAddress.restype = ctypes.c_void_p
        gl.wglGetProcAddress.argtypes = [ctypes.c_char_p]
        fn_ptr = gl.wglGetProcAddress(b"wglGetExtensionsStringEXT")
        if fn_ptr and gl.wglGetCurrentContext():
            get_ext = ctypes.cast(
                fn_ptr, ctypes.WINFUNCTYPE(ctypes.c_char_p))
            wgl_ext = (get_ext() or b"").decode("ascii", errors="replace")
    except Exception as exc:  # noqa: BLE001
        wgl_ext = f"<unavailable: {exc}>"

    cuda = CudaGL()
    gl_renderer = info.get("GL_RENDERER", "")
    device_match = cuda.device_name.split("(")[0].strip().lower() in gl_renderer.lower()

    # GL_TEXTURE_2D_ARRAY 注册探测（arrayIndex 映射，计划真机探测项）
    array_probe = {}
    try:
        arr_tex = ctx.texture_array((2, 64, 64), components=1, dtype="f1")
        res = cuda.register_image(arr_tex, GL_TEXTURE_2D_ARRAY)
        try:
            rc = cuda.map_one(res)
            if rc == 0:
                err, layer1 = cuda.mapped_array(res, array_index=1)
                array_probe = {
                    "register": "ok",
                    "map": "ok",
                    "layer1_array": "ok" if int(err) == 0 and layer1 else
                    f"rc={int(err)}",
                }
                cuda.unmap_one(res)
            else:
                array_probe = {"register": "ok", "map": f"rc={rc}"}
        finally:
            cuda.unregister(res)
            arr_tex.release()
    except Exception as exc:  # noqa: BLE001
        array_probe = {"error": f"{type(exc).__name__}: {exc}"}

    ctx.release()
    return {
        "gl_version": info.get("GL_VERSION"),
        "gl_renderer": gl_renderer,
        "gl_vendor": info.get("GL_VENDOR"),
        "cuda_driver_version": cuda.driver_version,
        "cuda_device_name": cuda.device_name,
        "interop_ctx_kind": cuda.ctx_kind,
        "device_match_gl_vs_cuda": device_match,
        "wgl_nv_dx_interop": "WGL_NV_DX_interop" in wgl_ext,
        "wgl_nv_dx_interop2": "WGL_NV_DX_interop2" in wgl_ext,
        "wgl_extensions_head": wgl_ext[:300],
        "texture2d_array_probe": array_probe,
    }


def _reference_planes(path: Path, n_frames: int) -> list[dict]:
    """同解码器（cuda）下载参考：紧凑 Y/UV 平面字节 + 几何。"""
    out = []
    c = av.open(str(path), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
    try:
        it = c.decode(c.streams.video[0])
        for i in range(n_frames):
            frame = next(it)
            fmt = frame.format.name
            layout = _plane_layout(fmt, frame.width, frame.height)
            planes = []
            for idx, p in enumerate(layout["planes"]):
                mv = _plane_mv(frame.planes[idx])
                pitch = frame.planes[idx].line_size
                planes.append({
                    "compact": _rows(mv, pitch, p["row_bytes"], p["rows"]),
                    "pitch": pitch,
                })
            out.append({
                "fmt": fmt, "width": frame.width, "height": frame.height,
                "y": planes[0], "uv": planes[1], "layout": layout,
            })
    finally:
        c.close()
    return out


def _resident_frames(path: Path, n_frames: int) -> tuple[list, list]:
    """cuda 驻留帧 + DLPack 信息（解析纯 Python，不触 CUDA API）。"""
    frames, infos = [], []
    c = av.open(str(path), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=True))
    try:
        it = c.decode(c.streams.video[0])
        for i in range(n_frames):
            frame = next(it)
            caps = [frame.planes[0].__dlpack__(), frame.planes[1].__dlpack__()]
            infos.append([_parse_dlpack_capsule(cap) for cap in caps])
            frames.append((frame, caps))
    finally:
        c.close()
    return frames, infos


def run_matrix() -> dict:
    """NV12 + P010 的 cuda→GL 拷贝正确性（readback md5 + FBO 采样对拍）。

    时序按 B1.1 安全序：先完成两个解码会话（驻留帧持引用、容器 close），
    再做 GL 注册拷贝——交织时序由 timing 阶段单独验证。
    """
    ctx = moderngl.create_standalone_context()
    out: dict = {}
    for name, fname in (("nv12_4k_h264", "4K_H264.mov"),
                        ("p010_4k_h265", "4K_H265.mov")):
        path = TEST_VIDEOS / fname
        if not path.exists():
            out[name] = {"status": "missing_asset", "path": str(path)}
            continue
        n = 4
        ref = _reference_planes(path, n)
        frames, infos = _resident_frames(path, n)
        if not ref or not infos:
            out[name] = {"status": "error", "error": "no frames decoded"}
            continue

        fmt = ref[0]["fmt"]
        layout = ref[0]["layout"]
        w, h = ref[0]["width"], ref[0]["height"]
        textures = _make_textures(ctx, layout)

        cuda = CudaGL()
        resources = [cuda.register_image(t, GL_TEXTURE_2D) for t in textures]

        readback = []
        sampled: dict = {}
        try:
            for i in range(len(ref)):
                for pi in range(2):
                    g = _plane_geometry(
                        infos[i][pi], layout["planes"][pi]["row_bytes"])
                    cuda.copy_to_texture(
                        resources[pi], g["device_ptr"], g["pitch"],
                        layout["planes"][pi]["row_bytes"],
                        layout["planes"][pi]["rows"])
                y_md5_ok = _md5(textures[0].read()) == _md5(ref[i]["y"]["compact"])
                uv_md5_ok = _md5(textures[1].read()) == _md5(ref[i]["uv"]["compact"])
                readback.append(bool(y_md5_ok and uv_md5_ok))
                if i == 0:
                    sampled = _sampled_fbo_check(
                        ctx, textures, ref[i]["y"]["compact"],
                        ref[i]["uv"]["compact"], w, h, layout["bit_depth"])
        finally:
            for r in resources:
                cuda.unregister(r)
            for t in textures:
                t.release()
        out[name] = {
            "status": "ok",
            "frames": len(ref),
            "fmt": fmt,
            "gl_textures": [
                f"{p['tex'][1]}x{p['tex'][0]}@{p['w']}x{p['h']}"
                for p in layout["planes"]
            ],
            "y_pitch_ref": ref[0]["y"]["pitch"],
            "y_pitch_resident": _plane_geometry(
                infos[0][0], layout["planes"][0]["row_bytes"])["pitch"],
            "uv_pitch_resident": _plane_geometry(
                infos[0][1], layout["planes"][1]["row_bytes"])["pitch"],
            "uv_dlpack_shape": infos[0][1]["shape"],
            "uv_dlpack_strides": infos[0][1]["strides"],
            "readback_md5_matches": f"{sum(readback)}/{len(readback)}",
            "sampled_fbo": sampled,
        }
    ctx.release()
    return out


# ---------------------------------------------------------------------------
# timing：一致终点「GL 纹理就绪可采样」三路径成对计时
# ---------------------------------------------------------------------------

def _bench_cpu_planar(path: Path, ctx, warmup: int, timed: int) -> dict:
    """生产 CPU planar 语义：软解（帧线程 AUTO，ffmpeg_source.py:1137-1147
    同款配置）+ 行提取 + 3 平面写纹理。"""
    c = av.open(str(path))
    stream = c.streams.video[0]
    stream.thread_type = "AUTO"
    stream.codec_context.thread_count = 0  # 自动选择
    open_mode = "thread_type=AUTO/thread_count=0"
    dec_ms: list[float] = []
    xfer_ms: list[float] = []
    cpu_bytes = 0
    textures = None
    layout_logged = None
    try:
        it = c.decode(c.streams.video[0])
        i = 0
        while i < warmup + timed:
            t0 = time.perf_counter_ns()
            frame = next(it)
            t1 = time.perf_counter_ns()
            fmt = frame.format.name
            if fmt in ("yuv420p", "yuv420p10le", "yuv420p16le"):
                depth = 16 if "16" in fmt else (10 if "10" in fmt else 8)
                dtype = np.dtype("<u2") if depth > 8 else np.dtype("u1")
                w, h = frame.width, frame.height
                planes = []
                for pi, (pw, ph) in enumerate(
                        ((w, h), (w // 2, h // 2), (w // 2, h // 2))):
                    mv = _plane_mv(frame.planes[pi])
                    pitch = frame.planes[pi].line_size
                    planes.append(_rows(
                        mv, pitch, pw * dtype.itemsize, ph))
            else:
                # 非预期格式：按契约布局兜底记录后跳出
                layout_logged = f"unexpected_fmt:{fmt}"
                break
            t2 = time.perf_counter_ns()
            if i >= warmup:
                if textures is None:
                    dt = "u2" if depth > 8 else "f1"
                    textures = [
                        ctx.texture((w, h), components=1, dtype=dt),
                        ctx.texture((w // 2, h // 2), components=1, dtype=dt),
                        ctx.texture((w // 2, h // 2), components=1, dtype=dt),
                    ]
                for pi, data in enumerate(planes):
                    textures[pi].write(data)
                cpu_bytes += sum(len(p) for p in planes)
                t3 = time.perf_counter_ns()
                dec_ms.append((t1 - t0) / 1e6)
                xfer_ms.append((t3 - t2) / 1e6)
            i += 1
    finally:
        c.close()
        if textures:
            for t in textures:
                t.release()
    return {
        "decode_ms": dec_ms, "xfer_ms": xfer_ms, "cpu_bytes": cpu_bytes,
        "texture_layout": layout_logged or "3 planes (Y,U,V)",
        "open_mode": open_mode,
    }


def _bench_hw_download(path: Path, ctx, warmup: int, timed: int) -> dict:
    """生产 HW download planar 语义：硬解+隐式下载（next 内）+ 行提取 +
    2 平面写纹理。"""
    textures = None
    c = av.open(str(path), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
    dec_ms: list[float] = []
    xfer_ms: list[float] = []
    cpu_bytes = 0
    try:
        it = c.decode(c.streams.video[0])
        i = 0
        while i < warmup + timed:
            t0 = time.perf_counter_ns()
            frame = next(it)
            t1 = time.perf_counter_ns()
            fmt = frame.format.name
            layout = _plane_layout(fmt, frame.width, frame.height)
            t2 = time.perf_counter_ns()
            if i >= warmup:
                if textures is None:
                    textures = _make_textures(ctx, layout)
                for pi, p in enumerate(layout["planes"]):
                    mv = _plane_mv(frame.planes[pi])
                    data = _rows(mv, frame.planes[pi].line_size,
                                 p["row_bytes"], p["rows"])
                    textures[pi].write(data)
                    cpu_bytes += len(data)
                t3 = time.perf_counter_ns()
                dec_ms.append((t1 - t0) / 1e6)
                xfer_ms.append((t3 - t2) / 1e6)
            i += 1
    finally:
        c.close()
        if textures:
            for t in textures:
                t.release()
    return {
        "decode_ms": dec_ms, "xfer_ms": xfer_ms, "cpu_bytes": cpu_bytes,
        "texture_layout": "2 planes (Y,UV) NV12-family",
    }


def _bench_hw_zero_copy(path: Path, ctx, warmup: int, timed: int) -> dict:
    """生产现实序：解码会话打开后**逐帧交织** DLPack 解析 +
    map/copy/unmap（interop 用私有上下文，主上下文不触碰）。"""
    c = av.open(str(path), hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=True))
    stream = c.streams.video[0]
    it = c.decode(stream)
    # 首帧拿几何（同 _verify_hw_output 语义：帧不浪费，warmup 从 0 计）。
    # 驻留帧 format.name == 'cuda'，契约布局（NV12/P010）从 DLPack 平面
    # itemsize 推导：1 → nv12，2 → p010le 家族。
    t0 = time.perf_counter_ns()
    first = next(it)
    t1 = time.perf_counter_ns()
    probe_cap = _parse_dlpack_capsule(first.planes[0].__dlpack__())
    layout = _plane_layout(
        "nv12" if probe_cap["itemsize"] == 1 else "p010le",
        first.width, first.height)

    cuda = CudaGL()  # 私有上下文，永不触碰主上下文
    textures = _make_textures(ctx, layout)
    resources = [cuda.register_image(t, GL_TEXTURE_2D) for t in textures]

    dec_ms: list[float] = []
    xfer_ms: list[float] = []
    interleave_errors: list[str] = []
    first_timed_md5: tuple[str, str] | None = None
    i = 0
    frame = first
    t_dec_end = t1
    try:
        while i < warmup + timed:
            t2 = time.perf_counter_ns()
            try:
                caps = [frame.planes[0].__dlpack__(),
                        frame.planes[1].__dlpack__()]
                infos = [_parse_dlpack_capsule(cap) for cap in caps]
            except Exception as exc:  # noqa: BLE001
                interleave_errors.append(
                    f"frame{i} dlpack: {type(exc).__name__}: {exc}")
                if len(interleave_errors) > 3:
                    break
                i += 1
                t0 = time.perf_counter_ns()
                frame = next(it)
                t_dec_end = time.perf_counter_ns()
                continue
            if i >= warmup:
                try:
                    for pi in range(2):
                        g = _plane_geometry(
                            infos[pi], layout["planes"][pi]["row_bytes"])
                        cuda.copy_to_texture(
                            resources[pi], g["device_ptr"], g["pitch"],
                            layout["planes"][pi]["row_bytes"],
                            layout["planes"][pi]["rows"])
                except Exception as exc:  # noqa: BLE001
                    interleave_errors.append(
                        f"frame{i} copy: {type(exc).__name__}: {exc}")
                    if len(interleave_errors) > 3:
                        break
                    i += 1
                    t0 = time.perf_counter_ns()
                    frame = next(it)
                    t_dec_end = time.perf_counter_ns()
                    continue
                t3 = time.perf_counter_ns()
                dec_ms.append((t_dec_end - t0) / 1e6)
                xfer_ms.append((t3 - t2) / 1e6)
                if first_timed_md5 is None:
                    # 交织序正确性抽查：首计时帧拷贝后立即读回
                    first_timed_md5 = (
                        _md5(textures[0].read()), _md5(textures[1].read()))
            i += 1
            if i < warmup + timed:
                t0 = time.perf_counter_ns()
                try:
                    frame = next(it)
                except Exception as exc:  # noqa: BLE001
                    interleave_errors.append(
                        f"frame{i} decode: {type(exc).__name__}: {exc}")
                    break
                t_dec_end = time.perf_counter_ns()
    finally:
        for r in resources:
            cuda.unregister(r)
        for t in textures:
            t.release()
        c.close()

    # 参考会话用 is_hw_owned=False（FFmpeg 私有 ctx 路径，免疫 -129 毒化）
    interleaved_match = None
    if first_timed_md5 is not None:
        try:
            ref_md5 = None
            rc = av.open(
                str(path),
                hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=False))
            try:
                rit = rc.decode(rc.streams.video[0])
                for k in range(warmup):
                    next(rit)
                rframe = next(rit)
                rlayout = _plane_layout(
                    rframe.format.name, rframe.width, rframe.height)
                parts = []
                for pi, p in enumerate(rlayout["planes"]):
                    mv = _plane_mv(rframe.planes[pi])
                    parts.append(_md5(_rows(
                        mv, rframe.planes[pi].line_size,
                        p["row_bytes"], p["rows"])))
                ref_md5 = (parts[0], parts[1])
            finally:
                rc.close()
            interleaved_match = first_timed_md5 == ref_md5
        except Exception as exc:  # noqa: BLE001
            interleaved_match = f"reference_failed: {type(exc).__name__}: {exc}"
    return {
        "decode_ms": dec_ms, "xfer_ms": xfer_ms, "cpu_bytes": 0,
        "texture_layout": "2 planes (Y,UV) NV12-family",
        "interleave_errors": interleave_errors[:5],
        "interleaved_content_match": interleaved_match,
    }


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {}
    a = np.array(xs)
    return {
        "mean_ms": round(float(a.mean()), 3),
        "p50_ms": round(float(np.percentile(a, 50)), 3),
        "max_ms": round(float(a.max()), 3),
    }


def run_timing(reps: int, warmup: int, timed: int) -> dict:
    ctx = moderngl.create_standalone_context()
    paths = {
        "4K_H264_nv12": TEST_VIDEOS / "4K_H264.mov",
        "4K_H265_p010": TEST_VIDEOS / "4K_H265.mov",
    }
    out = {"reps": reps, "warmup": warmup, "timed_frames": timed, "sources": {}}
    benchers = {
        "cpu_planar": _bench_cpu_planar,
        "hw_download_planar": _bench_hw_download,
        "hw_zero_copy": _bench_hw_zero_copy,
    }
    for src_name, path in paths.items():
        if not path.exists():
            out["sources"][src_name] = {"status": "missing_asset"}
            continue
        per_path: dict = {k: [] for k in benchers}
        for rep in range(reps):
            # 交错采样：轮换顺序消除漂移（计划 §1 成对采样）
            names = list(benchers)
            order = names[rep % len(names):] + names[: rep % len(names)]
            for name in order:
                try:
                    r = benchers[name](path, ctx, warmup, timed)
                except Exception as exc:  # noqa: BLE001
                    per_path[name].append({"error": f"{type(exc).__name__}: {exc}"})
                    continue
                total = [d + x for d, x in zip(r["decode_ms"], r["xfer_ms"])]
                per_path[name].append({
                    "decode": _stats(r["decode_ms"]),
                    "xfer": _stats(r["xfer_ms"]),
                    "total": _stats(total),
                    "steady_fps": round(1000.0 / float(np.mean(total)), 1)
                    if total else None,
                    "cpu_bytes": r["cpu_bytes"],
                    "texture_layout": r["texture_layout"],
                    "interleave_errors": r.get("interleave_errors"),
                    "interleaved_content_match": r.get(
                        "interleaved_content_match"),
                })
        summary = {}
        for name, runs in per_path.items():
            if runs and "error" in runs[-1] and len(runs[-1]) == 1:
                summary[name] = {"error": runs[-1]["error"]}
                continue
            fps = [x["steady_fps"] for x in runs if x.get("steady_fps")]
            summary[name] = {
                "fps_mean": round(float(np.mean(fps)), 1) if fps else None,
                "fps_min": round(float(np.min(fps)), 1) if fps else None,
                "fps_max": round(float(np.max(fps)), 1) if fps else None,
                "total_ms_mean": round(
                    float(np.mean([x["total"]["mean_ms"] for x in runs])), 3)
                if runs and runs[-1].get("total") else None,
                "decode_ms_mean": round(
                    float(np.mean([x["decode"]["mean_ms"] for x in runs])), 3)
                if runs and runs[-1].get("decode") else None,
                "xfer_ms_mean": round(
                    float(np.mean([x["xfer"]["mean_ms"] for x in runs])), 3)
                if runs and runs[-1].get("xfer") else None,
                "cpu_bytes_per_frame": round(
                    runs[-1]["cpu_bytes"] / timed, 1) if runs else None,
                "interleave_errors": runs[-1].get("interleave_errors"),
                "interleaved_content_match": runs[-1].get(
                    "interleaved_content_match"),
                "texture_layout": runs[-1]["texture_layout"],
                "per_rep": runs,
            }
        out["sources"][src_name] = summary
    ctx.release()
    return out


def run_limits(target: int = 256) -> dict:
    """注册资源数上限 + 全量 map/unmap + 恢复性（生产 present slot=24，
    目标 256 覆盖 128 帧对池的裕量）。"""
    ctx = moderngl.create_standalone_context()
    cuda = CudaGL()
    textures, resources = [], []
    first_fail = None
    t0 = time.perf_counter_ns()
    for i in range(target):
        tex = ctx.texture((64, 64), components=1, dtype="f1")
        try:
            resources.append(cuda.register_image(tex, GL_TEXTURE_2D))
            textures.append(tex)
        except Exception as exc:  # noqa: BLE001
            tex.release()
            first_fail = {"at": i, "error": f"{type(exc).__name__}: {exc}"}
            break
    t_reg_ms = (time.perf_counter_ns() - t0) / 1e6

    map_ok = False
    map_ms = 0.0
    map_mode = "per-resource"
    if resources:
        t0 = time.perf_counter_ns()
        rcs = [cuda.map_one(r) for r in resources]
        rcs2 = [cuda.unmap_one(r) for r in resources]
        map_ok = all(rc == 0 for rc in rcs) and all(rc == 0 for rc in rcs2)
        map_ms = (time.perf_counter_ns() - t0) / 1e6

    # 恢复性：全部 unregister 后重新注册一轮成功
    unreg_rcs = [cuda.unregister(r) for r in resources]
    recovery = "ok"
    try:
        tex = ctx.texture((64, 64), components=1, dtype="f1")
        r = cuda.register_image(tex, GL_TEXTURE_2D)
        cuda.unregister(r)
        tex.release()
    except Exception as exc:  # noqa: BLE001
        recovery = f"failed: {type(exc).__name__}: {exc}"
    for t in textures:
        t.release()
    ctx.release()
    return {
        "target": target,
        "registered": len(resources),
        "register_batch_ms": round(t_reg_ms, 1),
        "first_failure": first_fail,
        "map_unmap_mode": map_mode,
        "map_unmap_all_ok": map_ok,
        "map_unmap_all_ms": round(map_ms, 1),
        "unregister_nonzero_rcs": sum(1 for rc in unreg_rcs if rc != 0),
        "recovery_after_full_unregister": recovery,
    }


def _good_cycle_ok(ctx, cuda) -> dict:
    """恢复循环：注册不得改变纹理内容，注销后可再注册再读回。"""
    try:
        payload = bytes(range(256)) * 16
        tex = ctx.texture((64, 64), components=1, dtype="f1")
        tex.write(payload)
        res = cuda.register_image(tex, GL_TEXTURE_2D)
        try:
            during = _md5(tex.read())
        finally:
            cuda.unregister(res)
        after = _md5(tex.read())
        res2 = cuda.register_image(tex, GL_TEXTURE_2D)
        cuda.unregister(res2)
        tex.release()
        want = _md5(payload)
        return {
            "ok": during == want and after == want,
            "content_stable_while_registered": during == want,
            "content_stable_after_unregister": after == want,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_failures() -> dict:
    """进程内安全失效注入（每项后恢复循环）+ 子进程危险项。

    真机设备移除（TDR）无法在本进程安全模拟，按计划登记为后续人工项。
    """
    ctx = moderngl.create_standalone_context()
    cuda = CudaGL()
    tex = ctx.texture((64, 64), components=1, dtype="f1")
    tex.write(bytes(range(256)) * 16)
    res = cuda.register_image(tex, GL_TEXTURE_2D)
    results: dict = {}

    # f1: 非法 GL 纹理名注册
    try:
        err, bad = cu.cuGraphicsGLRegisterImage(0xFFFFFF, GL_TEXTURE_2D, 0)
        results["register_invalid_gl_name"] = {
            "rc": int(err), "err": cuda.err_str(err), "rejected": int(err) != 0}
    except Exception as exc:  # noqa: BLE001
        results["register_invalid_gl_name"] = {
            "exception": f"{type(exc).__name__}: {exc}", "rejected": True}

    # f2: 双重 unregister
    rc1 = cuda.unregister(res)
    rc2 = cuda.unregister(res)
    results["double_unregister"] = {
        "first_rc": rc1, "second_rc": rc2,
        "second_err": cuda.err_str(cu.CUresult(rc2)) if rc2 else "",
        "rejected": rc2 != 0}

    # f3: unregister NULL
    try:
        err, = cu.cuGraphicsUnregisterResource(0)
        results["unregister_null"] = {"rc": int(err),
                                      "err": cuda.err_str(err),
                                      "rejected": int(err) != 0}
    except Exception as exc:  # noqa: BLE001
        results["unregister_null"] = {
            "exception": f"{type(exc).__name__}: {exc}", "rejected": True}

    # f4: 坏 extent 拷贝（宽超数组 + 无效设备指针）不得损坏纹理
    tex2 = ctx.texture((64, 64), components=1, dtype="f1")
    tex2.write(b"\x11" * 4096)
    res2 = cuda.register_image(tex2, GL_TEXTURE_2D)
    try:
        rc_map = cuda.map_one(res2)
        rc = rc_map
        if rc_map == 0:
            try:
                err, mapped = cuda.mapped_array(res2)
                m = cu.CUDA_MEMCPY2D()
                m.srcMemoryType = cu.CUmemorytype.CU_MEMORYTYPE_DEVICE
                m.srcDevice = 0x10  # 无效设备指针
                m.srcPitch = 4096
                m.dstMemoryType = cu.CUmemorytype.CU_MEMORYTYPE_ARRAY
                m.dstArray = mapped
                m.WidthInBytes = 4096  # > 64 字节数组宽
                m.Height = 64
                err2, = cu.cuMemcpy2D(m)
                rc = int(err2)
            finally:
                cuda.unmap_one(res2)
        content = tex2.read()
        results["bad_extent_copy"] = {
            "rc": rc, "err": cuda.err_str(cu.CUresult(rc)) if rc else "",
            "rejected": rc != 0,
            "texture_unchanged": _md5(content) == _md5(b"\x11" * 4096)}
    finally:
        cuda.unregister(res2)
        tex2.release()

    # 恢复性：以上故障后正常注册/读回仍可用
    results["recovery_cycle"] = _good_cycle_ok(ctx, cuda)
    tex.release()
    ctx.release()

    # 子进程危险项：崩溃即证据，不影响父进程
    # - map_junk_handle：驱动不校验句柄（本机实测访问违例）——生产含义：
    #   句柄合法性由我方代码负责。
    # - ctx_poison_matrix：-129 毒化机制矩阵（B1.1 结论修正版）。
    for case in ("delete_tex_while_registered",
                 "ctx_release_while_registered",
                 "map_without_current_ctx",
                 "map_junk_handle",
                 "ctx_poison_matrix"):
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "--fault-child", case],
                capture_output=True, text=True, timeout=180)
            child_json = None
            for line in proc.stdout.splitlines():
                if line.startswith("FAULT_RESULT "):
                    child_json = json.loads(line[len("FAULT_RESULT "):])
            results[case] = {
                "returncode": proc.returncode,
                "result": child_json,
                "stderr_tail": proc.stderr[-300:],
            }
        except Exception as exc:  # noqa: BLE001
            results[case] = {"spawn_error": f"{type(exc).__name__}: {exc}"}
    return results


def run_fault_child(case: str) -> int:
    """子进程危险失效注入模式（--fault-child）。退出码恒 0/2，
    结果经 FAULT_RESULT JSON 行回传。"""
    result: dict = {"case": case}
    rc = 0
    try:
        ctx = moderngl.create_standalone_context()
        cuda = CudaGL()
        tex = ctx.texture((64, 64), components=1, dtype="f1")
        tex.write(b"\x33" * 4096)
        res = cuda.register_image(tex, GL_TEXTURE_2D)
        if case == "delete_tex_while_registered":
            tex.release()  # GL 侧删除注册中的纹理
            rc = cuda.unregister(res)
            result["unregister_rc"] = rc
            result["unregister_err"] = cuda.err_str(cu.CUresult(rc)) if rc else "no error"
        elif case == "ctx_release_while_registered":
            ctx.release()  # GL 上下文销毁，注册资源悬挂
            rc = cuda.unregister(res)
            result["unregister_rc"] = rc
            result["unregister_err"] = cuda.err_str(cu.CUresult(rc)) if rc else "no error"
        elif case == "map_without_current_ctx":
            err, = cu.cuCtxSetCurrent(None)
            result["ctx_unset_rc"] = int(err)
            rc = cuda.map_one(res)
            result["map_rc_after_unset"] = rc
            result["map_err"] = cuda.err_str(cu.CUresult(rc)) if rc else ""
            cuda.unregister(res)
        elif case == "map_junk_handle":
            # 驱动对句柄不做校验（本机实测访问违例）——预期本子进程异常/崩溃
            tex.release()
            try:
                err, = cu.cuGraphicsMapResources(1, 0xDEADBEEF, 0)
                result["map_junk_rc"] = int(err)
                result["survived"] = True
            except Exception as exc:  # noqa: BLE001
                result["exception"] = f"{type(exc).__name__}: {exc}"
                result["survived"] = False
        elif case == "ctx_poison_matrix":
            # -129 毒化机制矩阵（B1.1 结论修正版，独立进程避免污染主进程）
            def _open_owned() -> str:
                try:
                    cc = av.open(
                        "tests/test_videos/4K_H264.mov",
                        hwaccel=HWAccel(HWDeviceType.cuda, is_hw_owned=True))
                    n = sum(1 for _ in cc.decode(cc.streams.video[0]))
                    cc.close()
                    return f"OK({n}f)"
                except Exception as exc:  # noqa: BLE001
                    return f"FAIL({str(exc)[:40]})"

            result["fresh_open_hw_owned"] = _open_owned()
            cu.cuCtxSetCurrent(None)
            err, prim = cu.cuDevicePrimaryCtxRetain(cu.CUdevice(0))
            cu.cuCtxSetCurrent(prim)
            result["open_with_primary_current"] = _open_owned()
            cu.cuCtxSetCurrent(None)
            result["open_after_unbind"] = _open_owned()
            err, = cu.cuDevicePrimaryCtxReset(cu.CUdevice(0))
            result["reset_rc"] = int(err)
            result["open_after_reset"] = _open_owned()
        else:
            result["error"] = f"unknown case {case}"
            rc = 2
    except Exception as exc:  # noqa: BLE001
        result["exception"] = f"{type(exc).__name__}: {exc}"
        rc = 2
    print("FAULT_RESULT " + json.dumps(result, ensure_ascii=False))
    return 0 if rc == 0 else 2


# ---------------------------------------------------------------------------

def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="")
    parser.add_argument("--frames", type=int, default=100,
                        help="timing 每路径计时帧数")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--fault-child", default="",
                        help="（内部）子进程危险失效注入模式")
    args = parser.parse_args()

    if args.fault_child:
        return run_fault_child(args.fault_child)

    try:
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True,
                             cwd=str(PROJECT_ROOT))
        RESULT["git_commit"] = git.stdout.strip()
    except Exception:
        RESULT["git_commit"] = "unavailable"
    RESULT["args"] = {k: v for k, v in vars(args).items() if k != "fault_child"}

    phase("capability", run_capability)
    phase("matrix", run_matrix)
    phase("timing", lambda: run_timing(args.reps, 10, args.frames))
    phase("limits", run_limits)
    phase("failures", run_failures)

    RESULT["issues"] = ISSUES
    out = PROJECT_ROOT / "scripts" / (
        f"poc_cuda_gl_interop_{args.tag}.json" if args.tag
        else "poc_cuda_gl_interop.json")
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n结果已保存: {out}")
    print(f"issues={len(ISSUES)}")
    return 0 if not ISSUES else 2


if __name__ == "__main__":
    raise SystemExit(main())
