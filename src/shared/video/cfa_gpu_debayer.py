"""TRAW（Topos RAW）CFA 4 相位平面 GPU debayer 上传器（批 2，v1 双线性）。

数据流：ToposImageSequenceSource 解码出的 4×uint16 平面（R/Gr/Gb/B，各
W/2×H/2，12-bit PWL-log12 码值）→ R16UI 纹理 → 片元着色器：

    1. 双线性 debayer（RGGB 相位；texelFetch 精确 texel 寻址，边界 clamp）
    2. TC_TRANSFER_TRAW_LOG0 逆曲线 → 线性 e- 域（参数冻结 §3.2，与
       native/tools 同源公式，常量由 Python 侧 traw_log12_constants 计算）
    3. WB gain（线性域乘法，非破坏 uniform）+ EI/曝光（stops）
    4. 归一化（/满阱 FW）→ 可选 v1 预览 OOTF（gamma 2.2）→ RGBA16F

批 2 验收对照：UHD 序列时间线回放 ≥ 实时（GPU debayer）；WB 调整实时生效
（uniform 更新零重解码）。
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

import moderngl
import numpy as np

# ---- PWL-log12 曲线参数（topos_traw_format_plan §3.2，冻结） ----
TRAW_CURVE_TOE_E = 8.0        # S：线性趾锚点（e-）
TRAW_CURVE_FW_E = 16384.0     # FW：设计满阱（e-）
TRAW_CODE_MAX = 4095.0        # 12-bit 满码


def traw_log12_constants() -> Tuple[float, float, float]:
    """返回 (A, K, C0)——与 tools/bench_traw_pilot.py log12_fwd 同源公式。

    log 段：c = A·log2(1 + x/S)，A = CODE_MAX/log2(1+(FW−S)/S)
    线性趾：c = C0 + K·x，K = A/(ln2·2S)，C0 = A − K·S
    """
    s = TRAW_CURVE_TOE_E
    fw = TRAW_CURVE_FW_E
    a = TRAW_CODE_MAX / math.log2(1.0 + (fw - s) / s)
    k = a / (math.log(2.0) * (s + s))
    c0 = a - k * s
    return a, k, c0


_FULLSCREEN_VS = """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}
"""

# 双线性 debayer + LOG0 逆曲线 + WB/EI（RGGB；texelFetch 精确寻址）。
# 平面纹理尺寸 = (W/2, H/2)；输出全尺寸 W×H。
_DEBAYER_FS_TEMPLATE = """
#version 330

uniform usampler2D plane_r;
uniform usampler2D plane_gr;
uniform usampler2D plane_gb;
uniform usampler2D plane_b;
uniform vec2 out_size;        // 全尺寸 W×H（像素）
uniform float wb_gain_r;      // 线性域白平衡 gain（非破坏 uniform）
uniform float wb_gain_g;
uniform float wb_gain_b;
uniform float exposure_stops; // EI/曝光补偿（2^n）
uniform float black_level;    // RC8：码值域黑位（fetch 后减除，clamp 0）
uniform mat3 gamut_matrix;    // RC3：IDT gamut（camera-native→709；identity 默认）
uniform int gamma_mode;       // RC3：0=linear / 1=gamma2.2 / 2=sRGB
uniform float fw_e;           // 满阱（归一化分母）
uniform float curve_a;        // PWL-log12 常量（Python 同源注入）
uniform float curve_k;
uniform float curve_c0;
uniform float curve_s;

in vec2 v_texcoord;
out vec4 f_color;

float traw_log_inverse(float c) {
    // PWL-log12 逆映射（topos_traw_format_plan §3.2）：c<A 走线性趾，
    // 否则 log 段；结果 clamp 到 [0, FW]。
    float x;
    if (c < curve_a) {
        x = (c - curve_c0) / curve_k;
    } else {
        x = curve_s * (exp2(c / curve_a) - 1.0);
    }
    return clamp(x, 0.0, fw_e);
}

uint fetch(usampler2D tex, ivec2 tc, ivec2 size) {
    ivec2 c = clamp(tc, ivec2(0), size - ivec2(1));
    return texelFetch(tex, c, 0).r;
}

void main() {
    // 相位平面尺寸 = ceil(帧尺寸/2)（native pf=3 平面几何同为 ceil，
    // codec.c tc_frame_plane_geometry；偶数帧与 *0.5 截断等价）
    ivec2 psize = ivec2((out_size + 1.0) * 0.5);
    // 全分辨率输出像素中心 → 传感器整数坐标
    ivec2 pix = ivec2(floor(v_texcoord * out_size));
    pix = clamp(pix, ivec2(0), ivec2(out_size) - ivec2(1));
    int i = pix.x >> 1;
    int j = pix.y >> 1;
    int ex = pix.x & 1;
    int ey = pix.y & 1;
    ivec2 tc = ivec2(i, j);

    // RGGB 相位网格（平面索引 (i,j) → 传感器 (2i+ex, 2j+ey)）：
    //   R[j,i]=(2i,2j)  Gr[j,i]=(2i+1,2j)  Gb[j,i]=(2i,2j+1)  B[j,i]=(2i+1,2j+1)
    // 邻域方向按相位网格一致性取（边界 clamp）。
    float r;
    float b;
    float g;
    if (ex == 0 && ey == 0) {
        // R 位点：G = 右 Gr + 下 Gb；B = 4 对角平均
        r = float(fetch(plane_r, tc, psize));
        g = 0.5 * (float(fetch(plane_gr, tc, psize))
                   + float(fetch(plane_gb, tc, psize)));
        b = 0.25 * (float(fetch(plane_b, tc, psize))
                    + float(fetch(plane_b, tc - ivec2(1, 0), psize))
                    + float(fetch(plane_b, tc - ivec2(0, 1), psize))
                    + float(fetch(plane_b, tc - ivec2(1, 1), psize)));
    } else if (ex == 1 && ey == 0) {
        // Gr 位点：R = 左右平均；B = 上下平均
        r = 0.5 * (float(fetch(plane_r, tc, psize))
                   + float(fetch(plane_r, tc + ivec2(1, 0), psize)));
        g = float(fetch(plane_gr, tc, psize));
        b = 0.5 * (float(fetch(plane_b, tc, psize))
                   + float(fetch(plane_b, tc - ivec2(0, 1), psize)));
    } else if (ex == 0 && ey == 1) {
        // Gb 位点：R = 上下平均；B = 左右平均
        r = 0.5 * (float(fetch(plane_r, tc, psize))
                   + float(fetch(plane_r, tc + ivec2(0, 1), psize)));
        g = float(fetch(plane_gb, tc, psize));
        b = 0.5 * (float(fetch(plane_b, tc, psize))
                   + float(fetch(plane_b, tc - ivec2(1, 0), psize)));
    } else {
        // B 位点：G = 右上 Gr + 左 Gb；R = 4 对角平均
        b = float(fetch(plane_b, tc, psize));
        g = 0.5 * (float(fetch(plane_gr, tc, psize))
                   + float(fetch(plane_gb, tc, psize)));
        r = 0.25 * (float(fetch(plane_r, tc, psize))
                    + float(fetch(plane_r, tc + ivec2(1, 0), psize))
                    + float(fetch(plane_r, tc + ivec2(0, 1), psize))
                    + float(fetch(plane_r, tc + ivec2(1, 1), psize)));
    }

    // RC8：black level 减除（码值域，clamp 0 防负值进 log 段）
    r = max(r - black_level, 0.0);
    g = max(g - black_level, 0.0);
    b = max(b - black_level, 0.0);
    // LOG0 逆曲线 → 线性 e- 域 → WB/EI → 归一化
    vec3 lin = vec3(
        traw_log_inverse(r) * wb_gain_r,
        traw_log_inverse(g) * wb_gain_g,
        traw_log_inverse(b) * wb_gain_b);
    lin *= exp2(exposure_stops) / fw_e;
    // RC3：IDT gamut 变换（camera-native → BT.709 linear；非破坏 uniform，
    // identity 默认 = 行为零漂移）。变换在 clamp 前允许暂时超界。
    lin = gamut_matrix * lin;
    lin = clamp(lin, vec3(0.0), vec3(1.0));

    // RC3：查看伽马（display_ootf 布尔由 Python 侧折算进 gamma_mode）
    if (gamma_mode == 1) {
        lin = pow(lin, vec3(1.0 / 2.2));
    } else if (gamma_mode == 2) {
        vec3 hi = 1.055 * pow(max(lin, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
        lin = mix(lin * 12.92, hi, step(vec3(0.0031308), lin));
    }
    f_color = vec4(lin, 1.0);
}
"""


_LINEAR16_FS_TEMPLATE = """
#version 330

uniform usampler2D plane_r;
uniform usampler2D plane_gr;
uniform usampler2D plane_gb;
uniform usampler2D plane_b;
uniform vec2 out_size;
uniform float wb_gain_r;
uniform float wb_gain_g;
uniform float wb_gain_b;
uniform float exposure_stops;
uniform float black_level;    // RC8：码值域黑位（fetch 后减除，clamp 0）
uniform mat3 gamut_matrix;    // RC3：IDT gamut（camera-native→709；identity 默认）
uniform int gamma_mode;       // RC3：0=linear / 1=gamma2.2 / 2=sRGB

in vec2 v_texcoord;
out vec4 f_color;

uint fetch(usampler2D tex, ivec2 tc, ivec2 size) {
    ivec2 c = clamp(tc, ivec2(0), size - ivec2(1));
    return texelFetch(tex, c, 0).r;
}

void main() {
    // 相位平面尺寸 = ceil(帧尺寸/2)（与 12-bit 模板同式；native pf=3
    // 平面几何为 ceil，偶数帧与 *0.5 截断等价，奇数帧 floor 会漏采末列）
    ivec2 psize = ivec2((out_size + 1.0) * 0.5);
    ivec2 pix = ivec2(floor(v_texcoord * out_size));
    pix = clamp(pix, ivec2(0), ivec2(out_size) - ivec2(1));
    int i = pix.x >> 1;
    int j = pix.y >> 1;
    int ex = pix.x & 1;
    int ey = pix.y & 1;
    ivec2 tc = ivec2(i, j);

    float r;
    float b;
    float g;
    if (ex == 0 && ey == 0) {
        r = float(fetch(plane_r, tc, psize));
        g = 0.5 * (float(fetch(plane_gr, tc, psize))
                   + float(fetch(plane_gb, tc, psize)));
        b = 0.25 * (float(fetch(plane_b, tc, psize))
                    + float(fetch(plane_b, tc - ivec2(1, 0), psize))
                    + float(fetch(plane_b, tc - ivec2(0, 1), psize))
                    + float(fetch(plane_b, tc - ivec2(1, 1), psize)));
    } else if (ex == 1 && ey == 0) {
        r = 0.5 * (float(fetch(plane_r, tc, psize))
                   + float(fetch(plane_r, tc + ivec2(1, 0), psize)));
        g = float(fetch(plane_gr, tc, psize));
        b = 0.5 * (float(fetch(plane_b, tc, psize))
                   + float(fetch(plane_b, tc - ivec2(0, 1), psize)));
    } else if (ex == 0 && ey == 1) {
        r = 0.5 * (float(fetch(plane_r, tc, psize))
                   + float(fetch(plane_r, tc + ivec2(0, 1), psize)));
        g = float(fetch(plane_gb, tc, psize));
        b = 0.5 * (float(fetch(plane_b, tc, psize))
                   + float(fetch(plane_b, tc - ivec2(1, 0), psize)));
    } else {
        // B 位点：G = Gr + Gb 平均；R = 4 对角平均（与 12-bit 模板同式）
        b = float(fetch(plane_b, tc, psize));
        g = 0.5 * (float(fetch(plane_gr, tc, psize))
                   + float(fetch(plane_gb, tc, psize)));
        r = 0.25 * (float(fetch(plane_r, tc, psize))
                    + float(fetch(plane_r, tc + ivec2(1, 0), psize))
                    + float(fetch(plane_r, tc + ivec2(0, 1), psize))
                    + float(fetch(plane_r, tc + ivec2(1, 1), psize)));
    }

    // RC8：black level 减除（linear 域码值，clamp 0）
    r = max(r - black_level, 0.0);
    g = max(g - black_level, 0.0);
    b = max(b - black_level, 0.0);
    // 16-bit linear（批 4 raw16-*）：码值 = e- × 65535/FW，直接归一化，
    // 无 LOG0 逆曲线（transfer=linear 契约）。
    vec3 lin = vec3(r, g, b)
        * (vec3(wb_gain_r, wb_gain_g, wb_gain_b) * (1.0 / 65535.0));
    lin *= exp2(exposure_stops);
    // RC3：IDT gamut 变换（camera-native → BT.709 linear；identity 默认）
    lin = gamut_matrix * lin;
    lin = clamp(lin, vec3(0.0), vec3(1.0));
    if (gamma_mode == 1) {
        lin = pow(lin, vec3(1.0 / 2.2));
    } else if (gamma_mode == 2) {
        vec3 hi = 1.055 * pow(max(lin, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
        lin = mix(lin * 12.92, hi, step(vec3(0.0031308), lin));
    }
    f_color = vec4(lin, 1.0);
}
"""


class CfaGpuDebayer:
    """TRAW CFA GPU debayer（4×R16UI + 双线性 + LOG0 逆 + WB/EI uniform）。

    bit_depth=12（默认）走 PWL-log12 逆曲线（RAW Log 制作档）；
    bit_depth=16 走线性归一化（raw16-* linear 归档档，transfer=linear）。
    单 context 使用（与 YUVGPUUploader 同约束：只允许持锁的渲染线程调用）。
    """

    def __init__(self, ctx: moderngl.Context, dtype: str = 'f2',
                 bit_depth: int = 12) -> None:
        self._ctx = ctx
        self._dtype = dtype
        self._bit_depth = int(bit_depth)
        self._prog: Optional[moderngl.Program] = None
        self._vao: Optional[moderngl.VertexArray] = None
        self._vbo: Optional[moderngl.Buffer] = None
        self._textures: list[moderngl.Texture] = []
        self._plane_size: Tuple[int, int] = (0, 0)
        a, k, c0 = traw_log12_constants()
        self._curve = (a, k, c0, TRAW_CURVE_TOE_E, TRAW_CURVE_FW_E)

    # ---- shader/几何 ----

    def _ensure_program(self) -> moderngl.Program:
        if self._prog is None:
            fs = (_LINEAR16_FS_TEMPLATE if self._bit_depth >= 16
                  else _DEBAYER_FS_TEMPLATE)
            self._prog = self._ctx.program(
                vertex_shader=_FULLSCREEN_VS,
                fragment_shader=fs)
            vertices = np.array([
                -1.0, -1.0, 0.0, 0.0,
                1.0, -1.0, 1.0, 0.0,
                -1.0, 1.0, 0.0, 1.0,
                1.0, 1.0, 1.0, 1.0,
            ], dtype='f4')
            vbo = self._ctx.buffer(vertices.tobytes())
            self._vbo = vbo
            self._vao = self._ctx.vertex_array(
                self._prog, [(vbo, '2f 2f', 'in_vert', 'in_texcoord')])
        return self._prog

    # ---- 纹理 ----

    def _ensure_textures(self, pw: int, ph: int) -> list[moderngl.Texture]:
        if self._plane_size != (pw, ph) or len(self._textures) != 4:
            for t in self._textures:
                t.release()
            self._textures = [
                self._ctx.texture((pw, ph), components=1, dtype='u2')
                for _ in range(4)
            ]
            for t in self._textures:
                t.filter = (moderngl.NEAREST, moderngl.NEAREST)
                t.repeat_x = False
                t.repeat_y = False
            self._plane_size = (pw, ph)
        return self._textures

    def _write_plane(self, idx: int, plane: np.ndarray) -> None:
        data = np.ascontiguousarray(plane, dtype=np.uint16)
        self._textures[idx].write(data.tobytes())

    # ---- 主入口 ----

    def upload_and_render(
        self,
        planes: Sequence[np.ndarray],
        frame_width: int,
        frame_height: int,
        fbo: moderngl.Framebuffer,
        *,
        wb_gains: Tuple[float, float, float] = (1.0, 1.0, 1.0),
        exposure_stops: float = 0.0,
        display_ootf: bool = True,
        viewport: Optional[Tuple[int, int, int, int]] = None,
        black_level: float = 0.0,
        gamut_matrix: Optional[Sequence[float]] = None,
        gamma_mode: Optional[int] = None,
    ) -> None:
        """上传 4 相位平面并 debayer 渲染到 fbo。

        planes: R/Gr/Gb/B uint16（各 (W/2)×(H/2)；RGB 相位序见
            topos_image_source._COMPONENTS[3]）。
        wb_gains: 线性域乘性 gain（R, G, B）；非破坏 uniform，拖动零重解码。
        exposure_stops: EI/曝光补偿（+1 = ×2 线性）。
        display_ootf: v1 预览 OOTF（gamma 2.2）。默认开——线性域直出在
            无查看变换的管线里不可判读；导出/调色管线应显式关。RC3 起
            由 gamma_mode 承接（None 时按本开关折算），保留参数兼容旧调用。
        black_level: RC8 码值域黑位（fetch 后减除、clamp 0；默认 0 =
            不减除）。TRWM 携带源值（RC1）。
        gamut_matrix: RC3 IDT gamut（GLSL mat3 列主序扁平 9；camera-native
            → BT.709 linear，见 camera_gamut_matrices）。None = identity
            （行为零漂移）。
        gamma_mode: RC3 查看伽马（0=linear / 1=gamma2.2 / 2=sRGB）；None
            时按 display_ootf 折算（True→1）。
        """
        if len(planes) != 4:
            raise ValueError(f"TRAW 需要 4 相位平面，得到 {len(planes)}")
        pw = int(planes[0].shape[1])
        ph = int(planes[0].shape[0])
        expected = ((frame_width + 1) // 2, (frame_height + 1) // 2)
        if (pw, ph) != expected:
            raise ValueError(
                f"CFA 平面尺寸 {(pw, ph)} 与帧 {frame_width}×{frame_height} "
                f"不符（期望 {expected}）")
        prog = self._ensure_program()
        textures = self._ensure_textures(pw, ph)
        for idx, plane in enumerate(planes):
            self._write_plane(idx, plane)
        for idx, name in enumerate(('plane_r', 'plane_gr', 'plane_gb', 'plane_b')):
            textures[idx].use(idx)
            prog[name].value = idx
        prog['out_size'].value = (float(frame_width), float(frame_height))
        prog['wb_gain_r'].value = float(wb_gains[0])
        prog['wb_gain_g'].value = float(wb_gains[1])
        prog['wb_gain_b'].value = float(wb_gains[2])
        prog['exposure_stops'].value = float(exposure_stops)
        prog['black_level'].value = float(black_level)
        # RC3：IDT gamut（GLSL mat3 列主序注入；GLSL uniform 默认非
        # identity，必须显式给值——None → identity 零漂移）。旧
        # display_ootf 布尔参数折算进 gamma_mode（True→1），shader 不再
        # 声明该 uniform（编译器会剔除未使用量 → KeyError 防护）。
        if gamut_matrix is not None and len(gamut_matrix) == 9:
            prog['gamut_matrix'].value = tuple(float(v) for v in gamut_matrix)
        else:
            prog['gamut_matrix'].value = (1.0, 0.0, 0.0,
                                          0.0, 1.0, 0.0,
                                          0.0, 0.0, 1.0)
        mode = gamma_mode if gamma_mode is not None else (
            1 if display_ootf else 0)
        prog['gamma_mode'].value = int(mode)
        # LOG0 曲线 uniform 仅 12-bit 模板声明——16-bit linear 模板无这些
        # uniform，无条件注入会 KeyError（外层 except 吞掉 → 黑屏）
        if self._bit_depth < 16:
            prog['fw_e'].value = float(TRAW_CURVE_FW_E)
            prog['curve_a'].value = float(self._curve[0])
            prog['curve_k'].value = float(self._curve[1])
            prog['curve_c0'].value = float(self._curve[2])
            prog['curve_s'].value = float(TRAW_CURVE_TOE_E)
        # AUD-38705：fbo.use() 会把 context viewport 重绑为 FBO 自身
        # viewport，此前先设 ctx.viewport 再 fbo.use() 会被覆盖、形同
        # 虚设。顺序必须是记录前值 → fbo.use() → 设定 viewport → 渲染
        # → 恢复调用前的 viewport。
        prev_viewport = (
            self._ctx.viewport if viewport is not None else None)
        try:
            fbo.use()
            if viewport is not None:
                self._ctx.viewport = tuple(int(v) for v in viewport)
            self._vao.render(moderngl.TRIANGLE_STRIP)
        finally:
            if prev_viewport is not None:
                self._ctx.viewport = prev_viewport

    def release(self) -> None:
        for t in self._textures:
            t.release()
        self._textures = []
        self._plane_size = (0, 0)
        if self._vao is not None:
            self._vao.release()
            self._vao = None
        if self._vbo is not None:
            self._vbo.release()
            self._vbo = None
        if self._prog is not None:
            self._prog.release()
            self._prog = None
