"""
HDRI / IBL Baker — 把 .hdr/.exr 全景图预计算为 PBR 用的三张纹理。

输出三张纹理（全平台，纯 fragment shader 卷积，无需 compute shader）：
  - irradiance cubemap   漫反射辐照度（低分辨率，cosine 卷积）
  - prefilter cubemap    镜面预过滤（多 mip，每 mip 对应一个 roughness，GGX 重要性采样）
  - brdf_lut (2D)        (scale, bias) 查找表

平台约束：macOS OpenGL 上限 4.1，无 compute shader（#version 430）。
因此所有卷积都用 #version 330 fragment shader 渲染到 FBO 完成。

Cubemap 写入说明：ModernGL 5.12 的 ctx.framebuffer 不接受 texture_cube 作为
color attachment，且 texture_cube.write 不支持按面写入。因此用 raw PyOpenGL
直接分配（glTexStorage2D 带 mip）+ glFramebufferTexture2D 按面/按 mip 附件。
这一模式与 composite_viewport.py 里共享纹理拷贝的 raw-GL 用法一致。

参考：Epic Games, "Real Shading in Unreal Engine 4" (Siggraph 2013);
learnopengl.com/IBL。
"""

import logging
import os
from typing import Dict, Optional, Tuple

import numpy as np

from OpenGL import GL as gl
import moderngl

logger = logging.getLogger(__name__)

# ── 分辨率常量（IBL 通常低分辨率即可，卷积后看不出细节）──
_ENV_CUBEMAP_SIZE = 128      # equirect→cubemap 中间产物
_IRRADIANCE_SIZE = 32        # 漫反射辐照度（很低，是平滑的）
_PREFILTER_SIZE = 128        # 镜面预过滤（需要细节）
_PREFILTER_MIP_COUNT = 5     # log2(128)+1，对应 roughness 0..1
_PREFILTER_SAMPLES = 64      # 每像素 GGX 采样数（性能/质量权衡）
_BRDF_LUT_SIZE = 512

# 立方面朝向：OpenGL cubemap 规范。
# (target, forward, up) —— 用 look_at 构造该面的 view 矩阵（eye 在原点）。
# 每个面的 UV 坐标对应屏幕 NDC，shader 里据此重建方向 = normalize(forward + uv.x*right + uv.y*up)。
# 这里给的是面向；右向量用 forward × up 算出（右手系）。
_CUBEMAP_FACES = [
    # GL_TEXTURE_CUBE_MAP_POSITIVE_X  (+X 右)
    (gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X, ( 1.0,  0.0,  0.0), (0.0, -1.0,  0.0)),
    # GL_TEXTURE_CUBE_MAP_NEGATIVE_X   (−X 左)
    (gl.GL_TEXTURE_CUBE_MAP_NEGATIVE_X, (-1.0,  0.0,  0.0), (0.0, -1.0,  0.0)),
    # GL_TEXTURE_CUBE_MAP_POSITIVE_Y   (+Y 上)
    (gl.GL_TEXTURE_CUBE_MAP_POSITIVE_Y, ( 0.0,  1.0,  0.0), (0.0,  0.0,  1.0)),
    # GL_TEXTURE_CUBE_MAP_NEGATIVE_Y   (−Y 下)
    (gl.GL_TEXTURE_CUBE_MAP_NEGATIVE_Y, ( 0.0, -1.0,  0.0), (0.0,  0.0, -1.0)),
    # GL_TEXTURE_CUBE_MAP_POSITIVE_Z   (+Z 前)
    (gl.GL_TEXTURE_CUBE_MAP_POSITIVE_Z, ( 0.0,  0.0,  1.0), (0.0, -1.0,  0.0)),
    # GL_TEXTURE_CUBE_MAP_NEGATIVE_Z   (−Z 后)
    (gl.GL_TEXTURE_CUBE_MAP_NEGATIVE_Z, ( 0.0,  0.0, -1.0), (0.0, -1.0,  0.0)),
]

# 通用全屏 quad（NDC 坐标，TRIANGLE_STRIP）
_FULLSCREEN_VS = """
#version 330
out vec2 v_uv;
void main() {
    // 两个三角形覆盖 NDC [-1,1]
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0,
                  (gl_VertexID == 2) ? 3.0 : -1.0);
    v_uv = p * 0.5 + 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""

# 1) equirect → cubemap：按面方向采样 equirect 全景图
_EQUIRECT_TO_CUBE_FS = """
#version 330
in vec2 v_uv;
uniform sampler2D u_equirect;   // equirect 全景（linear RGB）
uniform vec3 u_forward;
uniform vec3 u_up;
uniform vec3 u_right;
layout(location = 0) out vec4 out_color;

const float PI = 3.14159265359;

void main() {
    // uv ∈ [0,1] → NDC 屏幕坐标（面内）
    vec2 ndc = v_uv * 2.0 - 1.0;
    // 面内方向（右手系）
    vec3 dir = normalize(u_forward + ndc.x * u_right + ndc.y * u_up);
    // 球面 → 经纬 UV
    float u = atan(dir.z, dir.x) / (2.0 * PI) + 0.5;  // 经度 → [0,1]
    float v = asin(clamp(dir.y, -1.0, 1.0)) / PI + 0.5; // 纬度 → [0,1]
    out_color = vec4(texture(u_equirect, vec2(u, v)).rgb, 1.0);
}
"""

# 2) irradiance cubemap：法线半球 cosine 卷积
#    用 Riemann 积分在球面网格上采样（精度足够 IBL 漫反射）。
_IRRADIANCE_CONVOLVE_FS = """
#version 330
in vec2 v_uv;
uniform samplerCube u_env;     // equirect→cubemap 产物
uniform vec3 u_forward;
uniform vec3 u_up;
uniform vec3 u_right;
layout(location = 0) out vec4 out_color;

const float PI = 3.14159265359;

void main() {
    vec2 ndc = v_uv * 2.0 - 1.0;
    vec3 N = normalize(u_forward + ndc.x * u_right + ndc.y * u_up);

    // 建立 N 的切线空间，在 (N) 上半球积分
    vec3 up = abs(N.y) < 0.999 ? vec3(0.0, 1.0, 0.0) : vec3(0.0, 0.0, 1.0);
    vec3 T = normalize(cross(up, N));
    vec3 B = cross(N, T);

    vec3 irradiance = vec3(0.0);
    float sample_count = 0.0;
    float phi_step = 0.025;   // 经度步长
    for (float phi = 0.0; phi < 2.0 * PI; phi += phi_step) {
        float cos_phi = cos(phi);
        float sin_phi = sin(phi);
        for (float theta = 0.0; theta < 0.5 * PI; theta += 0.025) {  // 只在上半球
            float sin_theta = sin(theta);
            float cos_theta = cos(theta);
            // 球面坐标 → 笛卡尔（切线空间）
            vec3 tangent = vec3(sin_theta * cos_phi, sin_theta * sin_phi, cos_theta);
            vec3 sample_dir = tangent.x * T + tangent.y * B + tangent.z * N;
            // 立体角面积元 dω = sinθ·dθ·dφ（球面 Riemann 求和），
            // 权重必须同时含 cosθ（Lambert 余弦项）与 sinθ（立体角项），
            // 缺 sinθ 会把极点样本超权/赤道欠权，漫反射 IBL 能量错误。
            irradiance += texture(u_env, sample_dir).rgb * cos_theta * sin_theta;
            sample_count += 1.0;
        }
    }
    irradiance = PI * irradiance / max(sample_count, 1.0);  // /N * π
    out_color = vec4(irradiance, 1.0);
}
"""

# 3) prefilter cubemap：GGX 重要性采样卷积（每 mip 一个 roughness）
#    用低离散采样（Hammersley 序列）+ GGX 分布采样方向。
_PREFILTER_CONVOLVE_FS = """
#version 330
in vec2 v_uv;
uniform samplerCube u_env;
uniform vec3 u_forward;
uniform vec3 u_up;
uniform vec3 u_right;
uniform float u_roughness;
uniform int   u_sample_count;
layout(location = 0) out vec4 out_color;

const float PI = 3.14159265359;

// Van der Corput 基序列
float radical_inverse_vdc(uint bits) {
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return float(bits) * 2.3283064365386963e-10;  // 1/2^32
}

// Hammersley 序列第 i 个点（共 N 个）
vec2 hammersley(uint i, uint N) {
    return vec2(float(i) / float(N), radical_inverse_vdc(i));
}

// GGX 重要性采样微表面法线
vec3 importance_sample_ggx(vec2 Xi, vec3 N, float roughness) {
    float a = roughness * roughness;
    float phi = 2.0 * PI * Xi.x;
    float cos_theta = sqrt((1.0 - Xi.y) / (1.0 + (a * a - 1.0) * Xi.y));
    float sin_theta = sqrt(1.0 - cos_theta * cos_theta);
    vec3 H = vec3(cos(phi) * sin_theta, sin(phi) * sin_theta, cos_theta);
    // 切线 → 世界
    vec3 up = abs(N.z) < 0.999 ? vec3(0.0, 0.0, 1.0) : vec3(1.0, 0.0, 0.0);
    vec3 T = normalize(cross(up, N));
    vec3 B = cross(N, T);
    return normalize(T * H.x + B * H.y + N * H.z);
}

void main() {
    vec2 ndc = v_uv * 2.0 - 1.0;
    vec3 N = normalize(u_forward + ndc.x * u_right + ndc.y * u_up);
    vec3 R = N;  // 镜面卷积假设 V=R=N（低 roughness 近似）
    float total_weight = 0.0;
    vec3 prefiltered = vec3(0.0);

    uint N_samples = uint(u_sample_count);
    for (uint i = 0u; i < 1024u; ++i) {  // 上界须为常量；内部用 break 截断
        if (i >= N_samples) break;
        vec2 Xi = hammersley(i, N_samples);
        vec3 H = importance_sample_ggx(Xi, N, u_roughness);
        vec3 L = normalize(2.0 * dot(H, R) * H - R);  // 反射
        float NdotL = max(dot(N, L), 0.0);
        if (NdotL > 0.0) {
            prefiltered += texture(u_env, L).rgb * NdotL;
            total_weight += NdotL;
        }
    }
    prefiltered = prefiltered / max(total_weight, 1e-4);
    out_color = vec4(prefiltered, 1.0);
}
"""

# 4) BRDF LUT (2D)：(NdotV, roughness) → (scale, bias)
_BRDF_LUT_FS = """
#version 330
in vec2 v_uv;   // x=NdotV∈[0,1], y=roughness∈[0,1]
uniform int u_sample_count;
layout(location = 0) out vec2 out_color;

const float PI = 3.14159265359;

float radical_inverse_vdc(uint bits) {
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return float(bits) * 2.3283064365386963e-10;
}

vec2 hammersley(uint i, uint N) {
    return vec2(float(i) / float(N), radical_inverse_vdc(i));
}

vec3 importance_sample_ggx(vec2 Xi, vec3 N, float roughness) {
    float a = roughness * roughness;
    float phi = 2.0 * PI * Xi.x;
    float cos_theta = sqrt((1.0 - Xi.y) / (1.0 + (a * a - 1.0) * Xi.y));
    float sin_theta = sqrt(1.0 - cos_theta * cos_theta);
    vec3 H = vec3(cos(phi) * sin_theta, sin(phi) * sin_theta, cos_theta);
    vec3 up = abs(N.z) < 0.999 ? vec3(0.0, 0.0, 1.0) : vec3(1.0, 0.0, 0.0);
    vec3 T = normalize(cross(up, N));
    vec3 B = cross(N, T);
    return normalize(T * H.x + B * H.y + N * H.z);
}

float geometry_schlick_ggx(float NdotV, float roughness) {
    float a = roughness;
    float k = (a * a) / 2.0;  // IBL 用 a²/2
    return NdotV / (NdotV * (1.0 - k) + k);
}

float geometry_smith(float NdotV, float NdotL, float roughness) {
    return geometry_schlick_ggx(NdotV, roughness) *
           geometry_schlick_ggx(NdotL, roughness);
}

vec2 integrate_brdf(float NdotV, float roughness) {
    vec3 V = vec3(sqrt(1.0 - NdotV * NdotV), 0.0, NdotV);
    float A = 0.0;
    float B = 0.0;
    vec3 N = vec3(0.0, 0.0, 1.0);
    uint N_samples = uint(u_sample_count);
    for (uint i = 0u; i < 1024u; ++i) {
        if (i >= N_samples) break;
        vec2 Xi = hammersley(i, N_samples);
        vec3 H = importance_sample_ggx(Xi, N, roughness);
        vec3 L = normalize(2.0 * dot(V, H) * H - V);
        float NdotL = max(L.z, 0.0);
        float NdotH = max(H.z, 0.0);
        float VdotH = max(dot(V, H), 0.0);
        if (NdotL > 0.0) {
            float G = geometry_smith(NdotV, NdotL, roughness);
            float G_vis = (G * VdotH) / (NdotH * NdotV + 1e-4);
            float Fc = pow(1.0 - VdotH, 5.0);
            A += (1.0 - Fc) * G_vis;
            B += Fc * G_vis;
        }
    }
    return vec2(A, B) / float(N_samples);
}

void main() {
    float NdotV = max(v_uv.x, 0.0);
    float roughness = clamp(v_uv.y, 0.0, 1.0);
    out_color = integrate_brdf(NdotV, roughness);
}
"""


def load_equirect(path: str) -> np.ndarray:
    """加载全景图，返回 (H,W,3) float32 线性 RGB。

    支持两种来源（OpenCV 读取，已是项目依赖）：
      - HDR 浮点格式（.hdr/.exr/.hdri）：cv2 返回 float32，值域天然合理（>1.0 高光），
        视为已线性，直接用。
      - LDR 整数格式（.jpg/.png/.bmp/.tiff/.webp）：cv2 返回 uint8/uint16（0-255 或
        0-65535），需归一化到 0-1 并做 sRGB→linear 转换（gamma 2.2 近似），否则 IBL
        卷积会严重过曝（LDR 值当线性直接用会放大 255 倍）。

    LDR 全景图当 IBL 用会丢失 >1.0 的高光信息（太阳/灯泡），反射偏暗偏色，
    但作为预览/简单环境光可接受。真实照片级 IBL 请用 .hdr/.exr。
    """
    import cv2

    # IMREAD_ANYDEPTH + IMREAD_ANYCOLOR 保留浮点 + 多通道
    img = cv2.imread(path, cv2.IMREAD_ANYDEPTH | cv2.IMREAD_ANYCOLOR)
    if img is None:
        raise ValueError(f"HDRI 加载失败（cv2 返回 None）: {path}")

    # 判断是否 LDR 整数格式（需归一化 + 去伽马）
    is_ldr = img.dtype != np.float32 and img.dtype != np.float16
    if img.dtype != np.float32:
        img = img.astype(np.float32)

    # AUD-88051：IMREAD_ANYCOLOR 读单通道（灰度）全景图返回 (H,W) 二维
    # 或 (H,W,1) 数组，旧代码直接访问 img.shape[2] 抛 IndexError（违背
    # docstring 承诺的结构化 ValueError），且后续 ctx.texture((w,h),3,...)
    # 也会因分量数不符失败。灰度复制三通道后再进颜色转换。
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=-1)
    elif img.shape[2] == 1:
        img = np.concatenate([img] * 3, axis=2)

    # OpenCV 默认 BGR → 转 RGB
    if img.shape[2] >= 3:
        img = img[:, :, :3]
        img = img[:, :, ::-1].copy()  # BGR→RGB

    # LDR 整数图：归一化到 0-1 + sRGB→linear（gamma 2.2 近似）
    if is_ldr:
        max_val = float(img.max()) if img.max() > 0 else 1.0
        # 常见位深：uint8→255, uint16→65535。按实际 max 归一化更稳健。
        if max_val > 1.0:
            img = img / max_val
        # sRGB→linear 近似（gamma 2.2）。精确的分段函数差异很小，不值得复杂度。
        img = np.power(np.clip(img, 0.0, 1.0), 2.2).astype(np.float32)

    return img


def _right_from_forward_up(forward: np.ndarray, up: np.ndarray) -> np.ndarray:
    """右手系：right = normalize(cross(forward, up))。

    forward/up 是面向量；screen-uv.x 沿 right，screen-uv.y 沿 up。
    """
    return _normalize(np.cross(forward, up))


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


class HDRIBaker:
    """把 equirect 全景图预计算为 PBR 用的 IBL 纹理集。

    线程模型：必须在 ModernGL context 所在线程调用（渲染线程）。
    生命周期：持有多张 GPU 纹理（cubemap × 2 + 2D × 1 + raw GL 名），
    调用 release() 释放。
    """

    def __init__(self) -> None:
        self._env_cube: Optional[object] = None    # 中间产物 cubemap（ModernGL 包裹）
        self._irradiance_cube: Optional[object] = None
        self._prefilter_cube: Optional[object] = None
        self._brdf_lut: Optional[object] = None
        # raw GL 对象名（cubemap 经 raw GL 分配，因 ModernGL 不能写 face）
        self._irradiance_glo = 0
        self._prefilter_glo = 0
        self._fbo_gl = 0       # 复用 FBO
        self._vaos: Dict[str, object] = {}  # 空属性 VAO（用 gl_VertexID），按 program 名分
        self._cache_key: Optional[Tuple] = None   # (path, mtime, bake 配置常量)
        # ModernGL program（卷积用）
        self._progs: Dict[str, object] = {}

    # ── 公共接口 ──────────────────────────────────────────────
    def bake(self, ctx, equirect_path: str) -> Tuple[object, object, object]:
        """加载全景图并预计算 IBL 纹理。

        结果按 (path, mtime, bake 配置常量) 缓存，相同输入不重 bake
        （AUD-88055：env cubemap 分辨率与 GGX 采样数均为模块级常量，不随
        输入图像分辨率变化，缓存键不含 size 维度）。
        返回 (irradiance_cube, prefilter_cube, brdf_lut) 三张 ModernGL 纹理。

        GL 状态契约（AUD-88055）：bake 过程会多次改写 viewport 与 FBO 绑定；
        返回（或抛出异常）前恢复进入时的 GL_VIEWPORT 与 FRAMEBUFFER 绑定，
        但不清空纹理绑定——调用方 bake 后必须自行 use 自己的目标 FBO。

        Args:
            ctx: ModernGL context（渲染线程）。
            equirect_path: .hdr/.exr 文件路径。

        Returns:
            (irradiance, prefilter, brdf_lut) —— ModernGL 纹理对象。
            irradiance/prefilter 是 samplerCube，brdf_lut 是 sampler2D。
        """
        if not equirect_path or not os.path.isfile(equirect_path):
            # 无文件：用全黑环境占位（PBR 仍可工作，仅 IBL 贡献为 0）
            self._ensure_black_placeholders(ctx)
            return self._irradiance_cube, self._prefilter_cube, self._brdf_lut

        try:
            mtime = os.path.getmtime(equirect_path)
        except OSError:
            mtime = 0.0
        key = (equirect_path, mtime, _ENV_CUBEMAP_SIZE, _PREFILTER_SAMPLES)
        if self._cache_key == key and self._irradiance_cube is not None:
            return self._irradiance_cube, self._prefilter_cube, self._brdf_lut

        equirect = load_equirect(equirect_path)  # (H,W,3) float32 linear
        return self._bake_from_array(ctx, equirect, key, label=equirect_path)

    def bake_array(self, ctx, equirect: np.ndarray, key: str,
                   label: str = "") -> Tuple[object, object, object]:
        """从 numpy 数组直接预计算 IBL（绕过文件加载）。

        用于程序生成的默认环境光（如 procedural_hdri.generate_sky_equirect），
        无需文件、零版权。卷积流程与 bake() 完全一致，只是输入来自内存数组。

        Args:
            ctx: ModernGL context（渲染线程）。
            equirect: (H,W,3) float32 线性 RGB 全景图。
            key: 缓存键（用固定字符串区分于文件加载，如 DEFAULT_SKY_KEY）。
            label: 日志/调试用的来源描述（可选）。

        Returns:
            同 bake()。
        """
        full_key = (key, 0, _ENV_CUBEMAP_SIZE, _PREFILTER_SAMPLES)
        if self._cache_key == full_key and self._irradiance_cube is not None:
            return self._irradiance_cube, self._prefilter_cube, self._brdf_lut
        return self._bake_from_array(ctx, equirect, full_key, label=label or key)

    def _bake_from_array(self, ctx, equirect: np.ndarray, key: tuple,
                         label: str = "") -> Tuple[object, object, object]:
        """共享卷积核心：从 (H,W,3) 数组烘焙 IBL（bake/bake_array 共用）。

        AUD-88055：进入时保存 GL_VIEWPORT 与 FRAMEBUFFER 绑定，bake 结束
        （正常返回或异常）在 finally 中恢复——bake 内部多次改写 viewport 并
        渲染到自己的 FBO，若停在 BRDF LUT 的 512×512 / bake FBO 上，调用方
        忘记重设时会画到错误区域。渲染线程调用（raw GL）。
        """
        # 保存状态必须在 release() 之前——删除当前绑定的 FBO 会把绑定重置为 0，
        # 之后就无法恢复调用方原本的绑定了。
        entry_viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
        entry_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        try:
            # 新输入：释放旧资源
            self.release()

            logger.info("HDRI bake: %s (env=%d irr=%d pref=%d pref_mip=%d samples=%d)",
                        label, _ENV_CUBEMAP_SIZE, _IRRADIANCE_SIZE,
                        _PREFILTER_SIZE, _PREFILTER_MIP_COUNT, _PREFILTER_SAMPLES)

            env_tex = self._upload_equirect(ctx, equirect)

            # 1) equirect → env cubemap
            self._env_cube = self._render_env_cubemap(ctx, env_tex)

            # 2) irradiance cubemap
            self._irradiance_cube = self._convolve_irradiance(ctx)

            # 3) prefilter cubemap（多 mip）
            self._prefilter_cube = self._convolve_prefilter(ctx)

            # 4) BRDF LUT（2D，只算一次）
            self._brdf_lut = self._integrate_brdf_lut(ctx)

            env_tex.release()
            self._cache_key = key
            # 清空 bake 过程遗留的 GL 错误（FBO 渲染/cubemap 卷积可能留下错误标志，
            # 会被 PyOpenGL 错误检查器误报到后续 _RawCube.use() 的 glActiveTexture 上）
            while gl.glGetError() != gl.GL_NO_ERROR:
                pass
            return self._irradiance_cube, self._prefilter_cube, self._brdf_lut
        finally:
            # AUD-88055：恢复进入时的 viewport 与 draw FBO 绑定（纹理绑定不在
            # 契约内，调用方 bake 后自行 use 自己的目标）。
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, entry_fbo)
            gl.glViewport(int(entry_viewport[0]), int(entry_viewport[1]),
                          int(entry_viewport[2]), int(entry_viewport[3]))

    def get_prefilter_max_lod(self) -> float:
        """prefilter cubemap 最大 mip 层数（供主 shader 的 textureLod）。"""
        return float(_PREFILTER_MIP_COUNT - 1)

    def release(self) -> None:
        """释放所有 GPU 资源。重复调用安全。"""
        # ModernGL 纹理（brdf_lut / 占位 cubemap）
        for tex in (self._brdf_lut,):
            if tex is not None:
                try:
                    tex.release()
                except Exception:
                    pass
        # raw-GL cubemap（env/irradiance/prefilter 都是 _RawCube；占位是 ModernGL texture_cube）
        for cube in (self._env_cube, self._irradiance_cube, self._prefilter_cube):
            if cube is not None:
                try:
                    cube.release()
                except Exception:
                    pass
        # 兜底：确保 raw glo 名被回收（_RawCube.release 已删，但占位 cubemap 走 ModernGL）
        for attr in ("_irradiance_glo", "_prefilter_glo"):
            glo = getattr(self, attr, 0)
            if glo:
                try:
                    gl.glDeleteTextures([glo])
                except Exception:
                    pass
                setattr(self, attr, 0)
        self._env_cube = None
        self._irradiance_cube = None
        self._prefilter_cube = None
        self._brdf_lut = None
        if self._fbo_gl:
            try:
                gl.glDeleteFramebuffers([self._fbo_gl])
            except Exception:
                pass
            self._fbo_gl = 0
        # 释放 VAO / program（ModernGL 对象）
        for vao in self._vaos.values():
            try:
                vao.release()
            except Exception:
                pass
        for prog in self._progs.values():
            try:
                prog.release()
            except Exception:
                pass
        self._vaos.clear()
        self._progs.clear()
        self._cache_key = None

    # ── 内部：程序/VAO ─────────────────────────────────────────
    def _prog(self, ctx, name: str, fs: str):
        if name not in self._progs:
            self._progs[name] = ctx.program(vertex_shader=_FULLSCREEN_VS,
                                            fragment_shader=fs)
        return self._progs[name]

    def _vao_for(self, ctx, name: str, fs: str):
        """空 VAO（按 program 名缓存）：vertex shader 用 gl_VertexID 生成顶点，无属性。

        VAO 绑定了 program，故每个卷积 program 需各自的空 VAO。
        ModernGL 允许 content=[] 的空 VAO。
        """
        if name not in self._vaos:
            self._vaos[name] = ctx.vertex_array(self._prog(ctx, name, fs), [])
        return self._vaos[name]

    def _ensure_fbo(self) -> int:
        # 清空遗留 GL 错误，避免 glGenFramebuffers 误报
        while gl.glGetError() != gl.GL_NO_ERROR:
            pass
        if not self._fbo_gl:
            self._fbo_gl = int(gl.glGenFramebuffers(1))
        return self._fbo_gl

    # ── 内部：上传 / 渲染到 face ───────────────────────────────
    def _upload_equirect(self, ctx, equirect: np.ndarray):
        h, w = equirect.shape[:2]
        tex = ctx.texture((w, h), 3, dtype='f4')
        tex.filter = (gl.GL_LINEAR, gl.GL_LINEAR)
        tex.write(np.ascontiguousarray(equirect).tobytes())
        return tex

    def _set_face_uniforms(self, prog, forward, up) -> None:
        right = _right_from_forward_up(forward, up)
        prog['u_forward'].value = (float(forward[0]), float(forward[1]), float(forward[2]))
        prog['u_up'].value = (float(up[0]), float(up[1]), float(up[2]))
        prog['u_right'].value = (float(right[0]), float(right[1]), float(right[2]))

    def _render_to_face(self, vao, size, target, glo, mip=0) -> None:
        """把当前 program 的 VAO 渲染到指定 cube face（target）+ mip。"""
        fbo = self._ensure_fbo()
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  target, glo, mip)
        # viewport 适配该 mip
        mip_size = max(size >> mip, 1)
        gl.glViewport(0, 0, mip_size, mip_size)
        gl.glDisable(gl.GL_DEPTH_TEST)
        # 空 VAO（无属性）需显式给顶点数：大三角形技巧用 3 个顶点
        vao.render(moderngl.TRIANGLE_STRIP, vertices=3)

    def _render_env_cubemap(self, ctx, env_tex):
        """equirect → env cubemap（中间产物，后续卷积采样它）。"""
        size = _ENV_CUBEMAP_SIZE
        prog = self._prog(ctx, "equirect", _EQUIRECT_TO_CUBE_FS)
        vao = self._vao_for(ctx, "equirect", _EQUIRECT_TO_CUBE_FS)
        # 用 raw GL 分配 cubemap（单 mip 即可，env 是中间产物）。
        # 全程 raw GL：ModernGL 不能把 cubemap 当 FBO 颜色附件按面渲染。
        glo = self._alloc_cube_raw(size, levels=1)
        env_tex.use(0)
        prog['u_equirect'].value = 0
        for target, fwd, up in _CUBEMAP_FACES:
            self._set_face_uniforms(prog, np.array(fwd, dtype=np.float32),
                                    np.array(up, dtype=np.float32))
            self._render_to_face(vao, size, target, glo, mip=0)
        return _RawCube(glo, size)

    def _convolve_irradiance(self, ctx):
        size = _IRRADIANCE_SIZE
        prog = self._prog(ctx, "irradiance", _IRRADIANCE_CONVOLVE_FS)
        vao = self._vao_for(ctx, "irradiance", _IRRADIANCE_CONVOLVE_FS)
        glo = self._alloc_cube_raw(size, levels=1)
        self._env_cube.use(0)  # _RawCube.use 绑定到 GL_TEXTURE0
        prog['u_env'].value = 0
        for target, fwd, up in _CUBEMAP_FACES:
            self._set_face_uniforms(prog, np.array(fwd, dtype=np.float32),
                                    np.array(up, dtype=np.float32))
            self._render_to_face(vao, size, target, glo, mip=0)
        self._irradiance_glo = glo
        return _RawCube(glo, size)

    def _convolve_prefilter(self, ctx):
        size = _PREFILTER_SIZE
        prog = self._prog(ctx, "prefilter", _PREFILTER_CONVOLVE_FS)
        vao = self._vao_for(ctx, "prefilter", _PREFILTER_CONVOLVE_FS)
        glo = self._alloc_cube_raw(size, levels=_PREFILTER_MIP_COUNT)
        self._env_cube.use(0)
        prog['u_env'].value = 0
        prog['u_sample_count'].value = _PREFILTER_SAMPLES
        for mip in range(_PREFILTER_MIP_COUNT):
            roughness = mip / float(max(_PREFILTER_MIP_COUNT - 1, 1))
            prog['u_roughness'].value = roughness
            for target, fwd, up in _CUBEMAP_FACES:
                self._set_face_uniforms(prog, np.array(fwd, dtype=np.float32),
                                        np.array(up, dtype=np.float32))
                self._render_to_face(vao, size, target, glo, mip=mip)
        self._prefilter_glo = glo
        # 启用 trilinear 过滤（跨 mip 插值）
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, glo)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER,
                           gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        return _RawCube(glo, size)

    def _integrate_brdf_lut(self, ctx):
        size = _BRDF_LUT_SIZE
        prog = self._prog(ctx, "brdf", _BRDF_LUT_FS)
        prog['u_sample_count'].value = 1024
        tex = ctx.texture((size, size), 2, dtype='f4')
        fbo = ctx.framebuffer(color_attachments=[tex])
        fbo.use()
        gl.glViewport(0, 0, size, size)
        gl.glDisable(gl.GL_DEPTH_TEST)
        vao = ctx.vertex_array(prog, [])
        vao.render(moderngl.TRIANGLE_STRIP, vertices=3)
        tex.filter = (gl.GL_LINEAR, gl.GL_LINEAR)
        # AUD-88055：viewport/FBO 恢复由 _bake_from_array 的 finally 统一负责
        # （这里不再假设调用方立即 use 自己的目标）。
        return tex

    def _alloc_cube_raw(self, size: int, levels: int) -> int:
        """raw GL 分配 cubemap（带指定 mip 层级），返回纹理名。

        注：调用前先清空遗留 GL 错误（ModernGL ctx.texture/filter 等可能留下
        未消费的错误标志，PyOpenGL 的错误检查器会误报到后续第一个 raw GL 调用）。
        glTexStorage2D（不可变存储）在某些 GL 实现上可能失败，回退到 glTexImage2D。
        """
        # 清空遗留 GL 错误，避免误报
        while gl.glGetError() != gl.GL_NO_ERROR:
            pass
        glo = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, glo)
        # 优先用 glTexStorage2D（不可变存储，支持 mip）；失败回退 glTexImage2D
        try:
            gl.glTexStorage2D(gl.GL_TEXTURE_CUBE_MAP, levels, gl.GL_RGBA16F, size, size)
            err = gl.glGetError()
            if err != 0:
                raise RuntimeError(f"glTexStorage2D err=0x{err:x}")
        except Exception as exc_texstorage:
            logger.warning("HDRI: glTexStorage2D 失败 %s，回退 glTexImage2D", exc_texstorage)
            while gl.glGetError() != gl.GL_NO_ERROR:
                pass
            # 回退：为每个 face + 每个 mip 用 glTexImage2D 分配（可变存储）
            for mip in range(levels):
                mip_size = max(size >> mip, 1)
                for face in range(6):
                    target = gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face
                    gl.glTexImage2D(target, mip, gl.GL_RGBA16F,
                                    mip_size, mip_size, 0,
                                    gl.GL_RGBA, gl.GL_FLOAT, None)
            fb = gl.glGetError()
            if fb != 0:
                raise RuntimeError(f"glTexImage2D fallback err=0x{fb:x}")
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        return glo

    def _ensure_black_placeholders(self, ctx) -> None:
        """无 HDRI 文件时：生成 1×1 黑色占位纹理，让 PBR shader 采样不报错。"""
        if self._irradiance_cube is not None:
            return  # 已有占位
        black = np.zeros((1, 1, 4), dtype=np.float32)
        irr = ctx.texture_cube((1, 1), 4, dtype='f4')
        for i in range(6):
            irr.write(i, black.tobytes())  # texture_cube.write(face, data)
        pre = ctx.texture_cube((1, 1), 4, dtype='f4')
        for i in range(6):
            pre.write(i, black.tobytes())
        lut = ctx.texture((1, 1), 2, dtype='f4')
        lut.write(np.zeros((1, 1, 2), dtype=np.float32).tobytes())
        self._irradiance_cube = irr
        self._prefilter_cube = pre
        self._brdf_lut = lut


class _RawCube:
    """包装一个 raw-GL cubemap 名，提供 .use()/release() 接口以混入 ModernGL 流。

    ModernGL 5.12 的 ctx.texture_cube 不能按面写入，故 cubemap 用 raw GL 分配，
    再用本类伪装成可 .use() 的对象供 PBR 主 shader 绑定。
    """

    def __init__(self, glo: int, size: int) -> None:
        self.glo = glo
        self.size = size

    def use(self, location: int = 0) -> None:
        """绑定到指定纹理单元（GL_TEXTURE0 + location）。"""
        # 清空遗留 GL 错误，避免 glActiveTexture 被误报（上游卷积/渲染可能留下错误标志）
        while gl.glGetError() != gl.GL_NO_ERROR:
            pass
        gl.glActiveTexture(gl.GL_TEXTURE0 + location)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, self.glo)

    def release(self) -> None:
        if self.glo:
            gl.glDeleteTextures([self.glo])
            self.glo = 0
