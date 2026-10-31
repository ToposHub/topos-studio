import numpy as np
import os
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from scipy.optimize import least_squares
    from scipy.interpolate import interp1d
    HAS_SCIPY = True
except ModuleNotFoundError:
    HAS_SCIPY = False
    least_squares = None
    interp1d = None

class GradingMath:
    """NumPy implementation of the GPU grading pipeline."""
    
    @staticmethod
    def rgb2hsv(rgb: np.ndarray) -> np.ndarray:
        # input rgb: (..., 3)
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        max_c = np.max(rgb, axis=-1)
        min_c = np.min(rgb, axis=-1)
        delta = max_c - min_c
        
        h = np.zeros_like(max_c)
        mask = delta > 1e-10
        
        # Red is max
        idx = (max_c == r) & mask
        h[idx] = (g[idx] - b[idx]) / delta[idx] % 6
        
        # Green is max
        idx = (max_c == g) & mask
        h[idx] = (b[idx] - r[idx]) / delta[idx] + 2
        
        # Blue is max
        idx = (max_c == b) & mask
        h[idx] = (r[idx] - g[idx]) / delta[idx] + 4
        
        h = h / 6.0
        s = np.zeros_like(max_c)
        s[mask] = delta[mask] / (max_c[mask] + 1e-10)
        v = max_c
        
        return np.stack([h, s, v], axis=-1)

    @staticmethod
    def hsv2rgb(hsv: np.ndarray) -> np.ndarray:
        h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        h = h * 6.0
        i = np.floor(h)
        f = h - i
        p = v * (1.0 - s)
        q = v * (1.0 - s * f)
        t = v * (1.0 - s * (1.0 - f))
        
        i = i.astype(int) % 6
        
        rgb = np.zeros_like(hsv)
        
        masks = [
            i == 0, i == 1, i == 2, i == 3, i == 4, i == 5
        ]
        
        rgb[masks[0]] = np.stack([v[masks[0]], t[masks[0]], p[masks[0]]], axis=-1)
        rgb[masks[1]] = np.stack([q[masks[1]], v[masks[1]], p[masks[1]]], axis=-1)
        rgb[masks[2]] = np.stack([p[masks[2]], v[masks[2]], t[masks[2]]], axis=-1)
        rgb[masks[3]] = np.stack([p[masks[3]], q[masks[3]], v[masks[3]]], axis=-1)
        rgb[masks[4]] = np.stack([t[masks[4]], p[masks[4]], v[masks[4]]], axis=-1)
        rgb[masks[5]] = np.stack([v[masks[5]], p[masks[5]], q[masks[5]]], axis=-1)
        
        return rgb

    @staticmethod
    def params_to_rgb(hue: float, sat: float, bright: float) -> np.ndarray:
        """Matches src/features/color/core/shader_compiler.py _params_to_rgb (was src/core/shader_compiler.py)"""
        stops = (bright ** 2) * 6.0
        if bright < 0: stops = -stops
        b_mult = np.power(2.0, stops)
        
        if sat == 0:
            return np.array([b_mult, b_mult, b_mult])
        
        h = hue / 60.0
        i = np.floor(h)
        f = h - i
        p = 1.0 * (1.0 - sat)
        q = 1.0 * (1.0 - sat * f)
        t = 1.0 * (1.0 - sat * (1.0 - f))
        
        ii = int(i) % 6
        if ii == 0: r, g, b = 1.0, t, p
        elif ii == 1: r, g, b = q, 1.0, p
        elif ii == 2: r, g, b = p, 1.0, t
        elif ii == 3: r, g, b = p, q, 1.0
        elif ii == 4: r, g, b = t, p, 1.0
        else: r, g, b = 1.0, p, q
            
        avg = max(1e-6, (r + g + b) / 3.0)
        return np.array([(r/avg) * b_mult, (g/avg) * b_mult, (b/avg) * b_mult])

    @staticmethod
    def apply_grading_prefix(color: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
        """Evaluate exposure through gamma, before cross-channel protection.

        The timeline LUT gate uses these unprotected responses to prove that
        the RGB-dependent dark-protection branch cannot run for any u8 input.
        """
        res = color.copy()
        dt = res.dtype

        # 1. Exposure
        exposure = params.get('exposure', 0.0)
        exp_stops = (exposure / 100.0) * (exposure / 100.0) * 6.0
        if exposure < 0.0: exp_stops = -exp_stops
        res *= dt.type(np.power(2.0, exp_stops))

        # 2. White Balance
        temp = params.get('temp', 0.0)
        tint = params.get('tint', 0.0)
        wb = np.array(
            [1.0 + temp / 200.0, 1.0 - tint / 200.0, 1.0 - temp / 200.0],
            dtype=dt)
        res *= wb

        # 3. Contrast
        contrast = params.get('contrast', 0.0)
        c_factor = 1.0 + contrast / 100.0
        if contrast != 0.0:
            # sign(res) * power(|res|/0.18 + 1e-10, c) * 0.18，单临时量
            mag = np.abs(res)
            mag /= dt.type(0.18)
            mag += dt.type(1e-10)
            np.power(mag, dt.type(c_factor), out=mag)
            np.sign(res, out=res)
            res *= mag
            res *= dt.type(0.18)

        # 4. Lift / Gamma / Gain
        l_hsv = params.get('lift', (0,0,0))
        gm_hsv = params.get('gamma', (0,0,0))
        gn_hsv = params.get('gain', (0,0,0))

        l_rgb = np.asarray(
            GradingMath.params_to_rgb(*l_hsv), dtype=dt)
        gn_rgb = np.asarray(
            GradingMath.params_to_rgb(*gn_hsv), dtype=dt)

        # Lift
        l_adj = (l_rgb - 1.0) * 0.2
        res *= gn_rgb
        l_factor = np.asarray(1.0, dtype=dt) - l_adj
        res *= l_factor
        res += l_adj

        # Gamma
        gamma_rgb = np.asarray(
            GradingMath.params_to_rgb(*gm_hsv), dtype=dt)
        safe_gamma = np.maximum(gamma_rgb, dt.type(0.01))
        gm_pow = np.power(dt.type(2.0), -np.log2(safe_gamma))

        # Apply Gamma with HDR extension（三分支公式与 np.where 双嵌套
        # 逐支等价，仅复用缓冲）
        mask_mid = (res > 0.0) & (res <= 1.0)
        mask_high = res > 1.0
        b_mid = np.maximum(res, dt.type(1e-10))
        np.power(b_mid, gm_pow, out=b_mid)
        b_high = res - dt.type(1.0)
        b_high *= gm_pow
        b_high += dt.type(1.0)
        b_low = res * gm_pow
        res = np.where(mask_mid, b_mid,
                       np.where(mask_high, b_high, b_low))
        return res

    @staticmethod
    def apply_grading(color: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
        """
        NumPy implementation of apply_grading snippet from shader_nodes.py
        color: (N, 3) float array
        params: dict of grading parameters

        2026-09-17 性能修订（语义不变）：全程保持输入 dtype（旧实现被
        float64 标量/params_to_rgb 升级，1080p 全链内存流量翻倍）并用
        就地运算削减全帧临时数组。导出实测 1080p 单帧 570→170ms。
        运算顺序与原实现逐条对应（同公式、同次序），仅联乘结合方式
        与 numpy 广播实现细节可能有 ~1ulp float 差异（atol=1e-6 测试
        基准内）。
        """
        res = GradingMath.apply_grading_prefix(color, params)
        dt = res.dtype

        # === 暗部保护（Lift/Gamma 暗部溢出）—— AUD-0912 ===
        # GPU 权威文本（shader_nodes.py GRADING_SNIPPET / shader_nodes_msl.py
        # :124-136）在 gamma 之后、shadows 之前有独立处理级：luma < 0 时对
        # < -0.5 的通道做 10:1 软压缩，再做 RGB 通道平衡。此前 CPU 前向
        # 模型缺失该级，暗部/负值场景拟合结果与 GPU 渲染系统性偏差。
        luma_weights = np.array([0.299, 0.587, 0.114], dtype=dt)
        luma_post_grading = res @ luma_weights
        dark_mask = luma_post_grading < 0.0
        if np.any(dark_mask):
            # 逐通道软压缩（保留 -0.5 范围）：x < -0.5 → -0.5 - |x+0.5|*0.1
            # RENDER-03/R29：深压缩门与逐像素 dark_mask 相交——GPU 权威
            # 文本是"当前像素 luma<0 才压缩"，此前仅按通道深浅判定，整
            # 幅图里任何一个暗像素出现就会把所有像素的深通道一并压缩
            #（审计实测同 HDR 像素 R 差 219 code）。
            deep_mask = (res < dt.type(-0.5)) & dark_mask[..., None]
            if np.any(deep_mask):
                res = np.where(
                    deep_mask,
                    dt.type(-0.5) - np.abs(res + dt.type(0.5)) * dt.type(0.1),
                    res,
                )
            # RGB 通道平衡：min/max 取压缩后通道，luma 基准取压缩前值
            #（与 GPU 语句顺序逐条一致）
            min_channel = np.min(res, axis=-1)
            max_channel = np.max(res, axis=-1)
            balance_mask = (
                (max_channel - min_channel > 0.15)
                & (min_channel < 0.0)
                & dark_mask
            )
            if np.any(balance_mask):
                balance_factor = np.clip(
                    np.abs(min_channel) * 0.25, 0.0, 0.15
                )
                t = np.expand_dims(balance_factor, axis=-1)
                luma_expanded = np.expand_dims(luma_post_grading, axis=-1)
                res = np.where(
                    np.expand_dims(balance_mask, axis=-1),
                    res * (1.0 - t) + luma_expanded * t,
                    res,
                )

        # 5. Shadows
        shadow = params.get('shadow', 0.0)
        if shadow != 0.0:
            shadow_factor = 1.0 + (shadow / 100.0)
            if shadow_factor > 0.0:
                clamped = np.clip(res, 0, 1)
                powered = np.power(clamped, dt.type(1.0 / shadow_factor))
                res = powered + (res - clamped)

        # 6. Highlights (Reinhard)
        highlights = params.get('highlights', 0.0)
        if highlights != 0.0:
            max_c = np.max(res, axis=-1, keepdims=True)
            mask = max_c > 0.1
            if np.any(mask):
                mapped_max = max_c.copy()
                if highlights < 0.0:
                    k = np.abs(highlights / 100.0) * 2.0
                    mapped_max[mask] = 0.1 + (max_c[mask] - 0.1) / (1.0 + k * (max_c[mask] - 0.1))
                else:
                    k = (highlights / 100.0) * 0.5
                    mapped_max[mask] = 0.1 + (max_c[mask] - 0.1) * (1.0 + k)

                ratio = (mapped_max[mask] / max_c[mask])[:, np.newaxis]
                # Identify the rows where mask is True
                row_indices = np.where(mask[..., 0])[0]
                res[row_indices] *= ratio

        # 7. Vibrance
        vibrance = params.get('vibrance', 0.0)
        if vibrance != 0.0:
            gray = np.dot(res, np.array(
                [0.299, 0.587, 0.114], dtype=dt))[..., np.newaxis]
            max_val = np.max(res, axis=-1, keepdims=True)
            sat = max_val - gray
            amt = vibrance / 100.0
            boost = amt * (1.0 - np.clip(sat, 0, 1))
            res = res * (1.0 + boost) + gray * (-boost)

        # 9. Hue Shift
        hue_shift = params.get('hue_shift', 0.0)
        if hue_shift != 0.0:
            # AUD-0913: GPU 两侧（shader_nodes.py:169 / shader_nodes_msl.py:206）
            # 直接用未 clip 的 color 做 rgb2hsv；此前 CPU 侧 np.clip(res, 0, 1)
            # 会在 HDR（>1）/负值区域与 GPU 渲染分叉。
            hsv = GradingMath.rgb2hsv(res)
            hsv[..., 0] = (hsv[..., 0] + hue_shift / 360.0) % 1.0
            res = GradingMath.hsv2rgb(hsv)

        # 10. Saturation
        saturation = params.get('saturation', 0.0)
        if saturation != 0.0:
            gray = np.dot(res, np.array(
                [0.299, 0.587, 0.114], dtype=dt))[..., np.newaxis]
            res -= gray
            res *= dt.type(1.0 + saturation / 100.0)
            res += gray

        return res

    @staticmethod
    def apply_soft_clip(color: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
        """Matches SOFT_CLIP_SNIPPET in shader_nodes.py.

        AUD-0911/0912 对齐：分支门控与 GPU 权威文本（shader_nodes.py
        SOFT_CLIP_SNIPPET / shader_nodes_msl.py MSL_SOFT_CLIP）逐条等价——
        - Toe 逐通道：``if (c < low && black_point < low)`` 软压缩，
          ``elif (c < black_point)`` 钳到 black_point（此前 bp >= low 时
          整段缺失）；
        - Toe 后的 RGB 通道平衡块（此前整体缺失）；
        - Shoulder：``if (max_c > high && white_point > high)`` 比例压缩，
          ``elif (max_c > white_point)`` 钳到 white_point（此前 wp <= high
          时整段缺失）。
        """
        if not params.get('enabled', False):
            return color

        res = color.copy()

        # 1. Exposure and Contrast Pre-adjustments
        exp = params.get('exposure', 0.0)
        contrast = params.get('contrast', 0.0)
        res *= np.power(2.0, exp)
        if contrast != 0.0:
            factor = (1.0 + contrast) if contrast >= 0.0 else (1.0 / (1.0 + abs(contrast)))
            res = np.sign(res) * np.power(np.abs(res) / 0.18 + 1e-10, factor) * 0.18

        # 2. Toe (Per-channel) —— GPU if/elif 逐元素等价
        low = params.get('low', 0.0)
        black_point = params.get('black_point', -0.1)
        toe_strength = params.get('toe_strength', 1.0)
        k_toe = max(toe_strength, 0.01)

        for i in range(3):
            c = res[:, i]
            toe_mask = (c < low) & bool(black_point < low)
            clamp_mask = (~toe_mask) & (c < black_point)
            if np.any(toe_mask):
                T, B = low, black_point
                rng = T - B
                diff = T - c[toe_mask]
                res[toe_mask, i] = B + rng * np.power(
                    1.0 + k_toe * diff / rng, -1.0 / k_toe)
            if np.any(clamp_mask):
                res[clamp_mask, i] = black_point

        # Toe 后 RGB 通道平衡（GPU "防止暗部 RGB 通道失衡导致色块" 块）
        toe_min = np.min(res, axis=-1)
        toe_max = np.max(res, axis=-1)
        balance_mask = (toe_max - toe_min > 0.2) & (toe_min < low)
        if np.any(balance_mask):
            balance_factor = np.clip(
                (toe_max - toe_min) * 0.25, 0.0, 0.15)
            toe_luma = res[balance_mask] @ np.array([0.299, 0.587, 0.114])
            mix_t = (balance_factor[balance_mask]
                     * np.abs(toe_min[balance_mask]))[:, np.newaxis]
            res[balance_mask] = (
                res[balance_mask] * (1.0 - mix_t)
                + toe_luma[:, np.newaxis] * mix_t
            )

        # 3. Shoulder (Ratio-preserving) —— GPU if/elif 逐元素等价
        high = params.get('high', 1.0)
        white_point = params.get('white_point', 1.1)
        shoulder_strength = params.get('shoulder_strength', 1.0)
        k_sh = max(shoulder_strength, 0.01)

        max_c = np.max(res, axis=-1)
        if white_point > high:
            mask = max_c > high
            if np.any(mask):
                T, W = high, white_point
                rng = W - T
                diff = max_c[mask] - T
                mapped_max = W - rng * np.power(
                    1.0 + k_sh * diff / rng, -1.0 / k_sh)
                scale = (mapped_max / max_c[mask])[:, np.newaxis]
                res[mask] *= scale
        else:
            # GPU elif 分支：max_c > white_point → 整体钳到 white_point
            mask = max_c > white_point
            if np.any(mask):
                scale = (white_point / max_c[mask])[:, np.newaxis]
                res[mask] *= scale

        return res

    @staticmethod
    def apply_mixer(color: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
        """Matches MIXER_SNIPPET in shader_nodes.py"""
        if not params:
            return color
            
        res = abs(color)
        sign_c = np.sign(color)
        hsv = GradingMath.rgb2hsv(res)
        h = hsv[..., 0] * 360.0
        
        centers = [0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 270.0, 300.0]
        color_names = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']
        
        total_hue_shift = np.zeros_like(h)
        sat_mult = np.ones_like(h)
        val_mult = np.ones_like(h)
        
        for i, name in enumerate(color_names):
            p = params.get(name, [0.0, 0.0, 0.0])
            if p[0] == 0 and p[1] == 0 and p[2] == 0:
                continue
                
            dist = np.abs(h - centers[i])
            dist = np.minimum(dist, 360.0 - dist)
            mask = np.clip(1.0 - (dist / 30.0), 0.0, 1.0)
            
            if p[0] != 0: total_hue_shift += p[0] * 0.3 * mask
            if p[1] != 0: sat_mult *= (1.0 + (p[1] / 100.0) * mask)
            if p[2] != 0: val_mult *= (1.0 + (p[2] / 100.0) * mask)
            
        hsv[..., 0] = (h + total_hue_shift) / 360.0 % 1.0
        hsv[..., 1] *= sat_mult
        hsv[..., 2] *= val_mult
        
        return sign_c * GradingMath.hsv2rgb(hsv)

class Decompiler:
    """LUT 反编译器：将 .cube 等 LUT 拟合回调色参数链。"""

    def __init__(self) -> None:
        self.lut_size = 0
        self.lut_data = None # (N, N, N, 3)

    def load_cube(self, path: str) -> Optional[np.ndarray]:
        # P1-COLOR-11: 解析收敛到统一 lut_loader（metadata/1D shaper/
        # DOMAIN 全支持），表布局保持 (B, G, R) 轴序
        from src.shared.color.lut_loader import load_lut
        lut = load_lut(path)
        self.lut_size = lut.size
        self.lut_data = lut.table
        return self.lut_data

    def decompile(self, lut_path: str) -> Optional[dict]:
        """反编译 LUT 为调色参数链（grading/soft_clip/mixer/curves/warper）。

        AUD-0914: 入口校验——路径不存在抛 ``FileNotFoundError``、LUT 尺寸
        不足抛 ``ValueError``，不再把 AttributeError/下游畸形错误漏给调用方。

        性能契约：三阶段 least_squares 重计算是秒级以上的 CPU 密集操作，
        **禁止在 UI 线程同步调用**；调用方（image_engine.decompile_lut）
        应经 worker 线程 + 进度/失败信号回 UI（引擎侧接线不在本模块）。

        Args:
            lut_path: .cube/.3dl 等 LUT 文件路径。

        Returns:
            参数 dict；scipy 缺失时返回 None（保留既有契约，错误经日志）。

        Raises:
            FileNotFoundError: ``lut_path`` 不存在或不是文件。
            ValueError: LUT 加载后尺寸 < 2（无法建立有效采样网格）。
        """
        if not HAS_SCIPY:
            logger.error("scipy is required for LUT decompilation. Install with: pip install scipy")
            return None

        # AUD-0914: 输入校验——坏路径给出明确领域异常，而不是让
        # load_lut/reshape 在深处抛出难定位的错误。
        if not isinstance(lut_path, str) or not lut_path:
            raise ValueError(f"decompile: invalid LUT path: {lut_path!r}")
        if not os.path.isfile(lut_path):
            raise FileNotFoundError(f"decompile: LUT file not found: {lut_path}")

        self.load_cube(lut_path)
        N = self.lut_size
        if self.lut_data is None or N < 2:
            raise ValueError(
                f"decompile: LUT '{lut_path}' has invalid size {N} (need >= 2)")

        # Identity Input Grid
        x = np.linspace(0, 1, N)
        grid_b, grid_g, grid_r = np.meshgrid(x, x, x, indexing='ij')
        input_grid = np.stack([grid_r, grid_g, grid_b], axis=-1).reshape(-1, 3)
        target_colors = self.lut_data.reshape(-1, 3)
        
        # Strategy Update: Multi-Stage Optimization (Curriculum Learning)
        # 1. Optimize Grading (Global Exposure/Contrast)
        # 2. Optimize Grading + Soft Clip (Tone Mapping / Highlight Control)
        # 3. Optimize Everything + Mixer (Color Fidelity)
        
        # Define base indices
        G_SIZE = 18
        SC_SIZE = 6

        def unpack_params(
                vec: np.ndarray) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
            # 1. Grading
            p = {
                'exposure': vec[0], 'temp': vec[1], 'tint': vec[2],
                'contrast': vec[3], 'saturation': vec[4], 'vibrance': vec[5],
                'shadow': vec[6], 'highlights': vec[7], 'hue_shift': vec[8],
                'lift': (vec[9], vec[10], vec[11]),
                'gamma': (vec[12], vec[13], vec[14]),
                'gain': (vec[15], vec[16], vec[17])
            }
            # 2. Soft Clip
            sc = {
                'enabled': True,
                'high': vec[18], 'white_point': vec[19], 'shoulder_strength': vec[20],
                'low': vec[21], 'black_point': vec[22], 'toe_strength': vec[23],
                'exposure': 0.0, 'contrast': 0.0
            }
            # 3. Mixer
            m = {}
            color_names = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']
            for i, name in enumerate(color_names):
                base = 24 + i * 3
                m[name] = [vec[base], vec[base+1], vec[base+2]]
            return p, sc, m

        def loss_func(vec: np.ndarray, input_colors: np.ndarray,
                      target_colors: np.ndarray) -> np.ndarray:
            p, sc, m = unpack_params(vec)
            pred = GradingMath.apply_grading(input_colors, p)
            pred = GradingMath.apply_soft_clip(pred, sc)
            pred = GradingMath.apply_mixer(pred, m)
            return (pred - target_colors).ravel()

        # Bounds Configuration
        low_g = [-100, -100, -100, -100, -100, -100, -100, -100, -180, 0, 0, -1, 0, 0, -1, 0, 0, -1]
        high_g = [100, 100, 100, 100, 100, 100, 100, 100, 180, 360, 1, 1, 360, 1, 1, 360, 1, 1]
        
        # Relaxed SC bounds to handle highlight compression
        low_sc = [0.1, 0.5, 0.05, -0.5, -1.0, 0.05]
        high_sc = [1.5, 5.0, 20.0, 0.5, 0.2, 20.0]
        
        low_m = [-100, -100, -100] * 8
        high_m = [100, 100, 100] * 8
        
        low = np.array(low_g + low_sc + low_m)
        high = np.array(high_g + high_sc + high_m)

        # Initial Guesses
        x_g = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        x_sc = [1.0, 1.1, 1.0, 0.0, -0.1, 1.0]
        x_m = [0.0] * 24
        
        indices = np.linspace(0, len(input_grid)-1, 4000, dtype=int)
        sample_in = input_grid[indices]
        sample_out = target_colors[indices]

        # --- Stage 1: Grading Only ---
        # Fixed SC and Mixer at default
        def loss_stage1(v_g: np.ndarray) -> np.ndarray:
            v_full = np.concatenate([v_g, x_sc, x_m])
            return loss_func(v_full, sample_in, sample_out)
            
        res1 = least_squares(loss_stage1, x_g, bounds=(low_g, high_g), max_nfev=100)
        x_g = res1.x

        # --- Stage 2: Grading + Soft Clip ---
        # Fixed Mixer at default
        def loss_stage2(v_g_sc: np.ndarray) -> np.ndarray:
            v_full = np.concatenate([v_g_sc, x_m])
            return loss_func(v_full, sample_in, sample_out)
            
        x_g_sc = np.concatenate([x_g, x_sc])
        res2 = least_squares(loss_stage2, x_g_sc, bounds=(low[:G_SIZE+SC_SIZE], high[:G_SIZE+SC_SIZE]), max_nfev=100)
        x_g_sc = res2.x
        x_g, x_sc = x_g_sc[:G_SIZE], x_g_sc[G_SIZE:]

        # --- Stage 3: Full Optimization ---
        x_full = np.concatenate([x_g, x_sc, x_m])
        res3 = least_squares(loss_func, x_full, bounds=(low, high), args=(sample_in, sample_out), max_nfev=300)
        
        v = res3.x
        final_params = {
            'exposure': float(v[0]), 'temp': float(v[1]), 'tint': float(v[2]), 
            'contrast': float(v[3]), 'saturation': float(v[4]), 'vibrance': float(v[5]), 
            'shadow': float(v[6]), 'highlights': float(v[7]), 'hue_shift': float(v[8]),
            'lift': (float(v[9]), float(v[10]), float(v[11])),
            'gamma': (float(v[12]), float(v[13]), float(v[14])),
            'gain': (float(v[15]), float(v[16]), float(v[17]))
        }
        final_sc = {
            'enabled': True,
            'high': float(v[18]), 'white_point': float(v[19]), 'shoulder_strength': float(v[20]),
            'low': float(v[21]), 'black_point': float(v[22]), 'toe_strength': float(v[23]),
            'exposure': 0.0, 'contrast': 0.0
        }
        final_mixer = {}
        color_names = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']
        for i, name in enumerate(color_names):
            base = 24 + i * 3
            final_mixer[name] = [float(v[base]), float(v[base+1]), float(v[base+2])]
            
        # Phase B: Curves (Residual Fix - Simplified to Identity)
        # With SC and Mixer, Curves are often redundant for LUT matching.
        curves_lut = np.zeros((256, 3), dtype=np.float32)
        curve_domain = np.linspace(0, 1, 256)
        for i in range(3):
            curves_lut[:, i] = curve_domain

        # Phase C: Warper
        # Calculate residual after all parametric steps
        pred_full = GradingMath.apply_grading(input_grid, final_params)
        pred_full = GradingMath.apply_soft_clip(pred_full, final_sc)
        pred_full = GradingMath.apply_mixer(pred_full, final_mixer)
        
        # Extract Warper Grid mapping Predicted -> Target
        warper_grid = self.extract_warper_grid(pred_full, target_colors, grid_size=12)
        warper_grid = np.flip(warper_grid, axis=0) # Flip Y axis
        
        return {
            'grading': final_params,
            'soft_clip': final_sc,
            'mixer': final_mixer,
            'curves': curves_lut,
            'warper': warper_grid
        }

    def extract_warper_grid(self, source_rgb: np.ndarray, target_rgb: np.ndarray, grid_size: int = 12) -> np.ndarray:
        # source_rgb: After Curves
        # target_rgb: Target LUT output
        # Warper grid is (grid_size, grid_size, 2)
        # Now stores (delta_hue, delta_sat) offsets instead of Cartesian coordinates.

        source_hsv = GradingMath.rgb2hsv(np.clip(source_rgb, 0, 1))
        target_hsv = GradingMath.rgb2hsv(np.clip(target_rgb, 0, 1))

        # Warper Grid structure:
        # X: Hue (0-1)
        # Y: Saturation (0-1)
        # Value: [delta_hue, delta_sat] — offset to apply

        deltas = np.zeros((grid_size, grid_size, 2), dtype=np.float32)

        # For each grid cell, compute average delta from source to target
        h_idx = np.clip((source_hsv[:, 0] * grid_size).astype(int), 0, grid_size - 1)
        s_idx = np.clip(((1.0 - source_hsv[:, 1]) * grid_size).astype(int), 0, grid_size - 1)

        # Compute per-pixel deltas
        dh = target_hsv[:, 0] - source_hsv[:, 0]
        # Handle hue wrap-around: take shortest path
        dh = np.where(dh > 0.5, dh - 1.0, np.where(dh < -0.5, dh + 1.0, dh))
        ds = target_hsv[:, 1] - source_hsv[:, 1]

        count = np.zeros((grid_size, grid_size))
        sum_dh = np.zeros((grid_size, grid_size))
        sum_ds = np.zeros((grid_size, grid_size))

        # Vectorized accumulation
        valid_mask = source_hsv[:, 1] > 0.05
        if np.any(valid_mask):
            np.add.at(sum_dh, (s_idx[valid_mask], h_idx[valid_mask]), dh[valid_mask])
            np.add.at(sum_ds, (s_idx[valid_mask], h_idx[valid_mask]), ds[valid_mask])
            np.add.at(count, (s_idx[valid_mask], h_idx[valid_mask]), 1)

        mask = count > 0
        deltas[mask, 0] = sum_dh[mask] / count[mask]
        deltas[mask, 1] = sum_ds[mask] / count[mask]

        return deltas

