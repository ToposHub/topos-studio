"""视频时域降噪：运动估计 + 状态管理 + 递归累积（GPU）。

对应计划 M3（运动估计）与 M4（时域降噪 V1）：

- TemporalDenoiseState: 纯 CPU 状态机（可脱离 GL 单元测试），实现 M4.4 的
  全部重置条件（换素材 / seek / 帧号不连续 / 参数变化 / 分辨率变化 / 世代变化）。
- VideoTemporalDenoiser: 渲染线程 GPU 实现。
  Preview（因果模式）：当前帧 + 历史纹理，运动补偿 warp 后自适应混合，
  历史即上一帧输出（ping-pong），无额外解码请求。
  Paused/Export（窗口模式）：给定有序邻帧纹理序列，顺序重放累积。

回退语义：GL 异常 / 无历史 / 帧不连续时直接返回原始纹理，不阻塞播放。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Hashable, Optional, Sequence, Tuple

import moderngl

from src.shared.video.denoise.params import TemporalDenoiseParams

from .video_denoise_gl import QuadRenderer, RenderSurface, RenderSurfaceCache
from . import video_denoise_shaders as vs

logger = logging.getLogger(__name__)

# 运动估计质量 → 1/4 分辨率搜索半径（fast/balanced/accurate）
_MOTION_SEARCH_RADIUS = {"fast": 3, "balanced": 4, "accurate": 5}

# frames(1/2/3) → 递归混合系数缩放（有效时域窗口随 frames 增大）
_FRAMES_BLEND_SCALE = {1: 0.8, 2: 1.0, 3: 1.25}


class TemporalDenoiseState:
    """时域降噪状态机（纯 CPU，可单元测试）。

    判定一段新帧上下文是否可以复用历史，以及历史已累积的帧数。
    """

    def __init__(self) -> None:
        self.clip_key: Optional[Hashable] = None
        self.generation_id: int = -1
        self.last_frame_index: int = -1
        self.last_delta: int = 0
        self.parameters_key: Optional[Tuple] = None
        self.size: Tuple[int, int] = (0, 0)
        self.accumulated_frames: int = 0
        self.scene_id: int = 0

    def reset(self, reason: str = "") -> None:
        if self.accumulated_frames > 0 and logger.isEnabledFor(logging.DEBUG):
            logger.debug("TemporalDenoiseState reset: %s", reason)
        self.last_frame_index = -1
        self.last_delta = 0
        self.accumulated_frames = 0

    def prepare(
        self,
        clip_key: Hashable,
        frame_index: int,
        generation_id: int,
        parameters_key: Tuple,
        size: Tuple[int, int],
    ) -> bool:
        """检查是否可复用历史；返回 True 表示历史有效可用。

        内部按 M4.4 条件执行重置，并推进状态。
        """
        reset_reason: Optional[str] = None

        if self.clip_key != clip_key:
            reset_reason = "clip_change"
        elif self.generation_id != generation_id:
            reset_reason = "generation_change(seek/clip-switch)"
        elif self.size != size:
            reset_reason = "size_change"
        elif self.parameters_key != parameters_key:
            reset_reason = "params_change"

        if reset_reason is None:
            if self.last_frame_index >= 0:
                delta = frame_index - self.last_frame_index
                if delta == 0:
                    # 同帧重绘（调色参数变化/viewport 刷新）：画面内容没变，
                    # 历史依然有效——复用而不重置，也不推进累积计数。
                    # AUD-1000: 判定显式要求已有历史（last_frame_index >= 0）。
                    # reset()/force_reset() 后 last_frame_index == -1，若落入
                    # delta==0 分支则状态机永久卡死（accumulated_frames 不再
                    # 推进，时域降噪静默失效），必须走下方完整推进路径。
                    return self.accumulated_frames > 1
                elif abs(delta) != 1:
                    reset_reason = f"frame_jump(delta={delta})"
                elif self.last_delta != 0 and delta != self.last_delta:
                    # 方向反转（正放↔倒放切换）：历史方向不再一致
                    reset_reason = "direction_change"

        if reset_reason is not None:
            self.reset(reset_reason)

        self.clip_key = clip_key
        self.generation_id = generation_id
        self.parameters_key = parameters_key
        self.size = size
        self.last_delta = frame_index - self.last_frame_index if self.last_frame_index >= 0 else 0
        self.last_frame_index = frame_index
        self.accumulated_frames += 1
        return self.accumulated_frames > 1

    def note_scene_cut(self) -> None:
        """外部（或 GPU 统计）检测到场景切换：清空历史帧数。"""
        self.scene_id += 1
        self.accumulated_frames = 1


class VideoTemporalDenoiser:
    """GPU 时域降噪（因果递归模式 + 窗口重放模式）。"""

    def __init__(self, ctx: moderngl.Context, quad: QuadRenderer,
                 surfaces: RenderSurfaceCache) -> None:
        self._ctx = ctx
        self._quad = quad
        self._surfaces = surfaces
        self._programs: Dict[str, moderngl.Program] = {}
        self.state = TemporalDenoiseState()
        # ping-pong 历史表面（在 surfaces 中以固定 key 管理）
        self._hist_index = 0
        self._prev_luma_valid = False
        self._last_motion_key = "temporal_motion"
        # 统计
        self.stats: Dict[str, Any] = {
            "frames_processed": 0,
            "resets": 0,
            "passthrough": 0,
        }

    # ------------------------------------------------------------------

    def _prog(self, name: str, fragment: str) -> Optional[moderngl.Program]:
        prog = self._programs.get(name)
        if prog is not None:
            return prog
        try:
            prog = self._ctx.program(vs.QUAD_VERTEX_SHADER, fragment)
        except Exception:
            logger.exception("VideoTemporalDenoiser: compile %s failed", name)
            return None
        self._programs[name] = prog
        return prog

    def force_reset(self) -> None:
        self.state.reset("manual")
        self._prev_luma_valid = False
        self._hist_index = 0
        self.stats["resets"] += 1

    # ------------------------------------------------------------------
    # 因果模式（Preview / 播放 / 暂停逐帧）

    def process(
        self,
        source_tex: moderngl.Texture,
        frame_index: int,
        generation_id: int,
        clip_key: Hashable,
        params: TemporalDenoiseParams,
        params_key: Tuple,
    ) -> moderngl.Texture:
        """处理当前帧；返回降噪输出纹理（无历史时返回原始纹理）。"""
        p = params.sanitized()
        if not p.is_active():
            self.stats["passthrough"] += 1
            return source_tex

        width, height = source_tex.size
        if width < 16 or height < 16:
            self.stats["passthrough"] += 1
            return source_tex

        history_usable = self.state.prepare(
            clip_key, frame_index, generation_id, params_key, (width, height)
        )

        try:
            return self._process_causal(
                source_tex, p, width, height, history_usable
            )
        except Exception:
            logger.exception("VideoTemporalDenoiser: causal failed, passthrough")
            self.force_reset()
            self.stats["passthrough"] += 1
            return source_tex

    def _process_causal(self, source_tex: moderngl.Texture, p: TemporalDenoiseParams,
                        width: int, height: int, history_usable: bool) -> moderngl.Texture:
        qw = max(1, width // 4)
        qh = max(1, height // 4)

        # 1. 当前帧亮度 1/4 降采样
        luma_cur = self._surfaces.get("temporal_luma_cur", qw, qh)
        if luma_cur is None:
            return source_tex
        self._downsample_luma(source_tex, luma_cur)

        # 2. 历史表面
        # AUD-0907: 缓存键不带尺寸——RenderSurfaceCache.get 在尺寸不匹配时
        # 自动释放重建；键内嵌尺寸会让每种出现过的分辨率各占一份 GPU 显存
        # 且永不淘汰（多分辨率时间线无上界累积）。
        hist_a = self._surfaces.get("temporal_hist_a", width, height)
        hist_b = self._surfaces.get("temporal_hist_b", width, height)
        out = self._surfaces.get("temporal_out", width, height)
        if hist_a is None or hist_b is None or out is None:
            return source_tex

        prev_hist = hist_a if self._hist_index == 0 else hist_b
        next_hist = hist_b if self._hist_index == 0 else hist_a

        if not history_usable or not self._prev_luma_valid:
            # 无有效历史：历史 = 当前帧拷贝，输出直通（空域仍可继续处理）
            next_hist.use()
            self._ctx.viewport = (0, 0, width, height)
            copy_prog = self._prog("copy", vs.COPY_FRAGMENT)
            if copy_prog is not None:
                source_tex.use(0)
                copy_prog["u_texture"].value = 0
                self._quad.draw(copy_prog)
            self._swap_history()
            self._store_prev_luma(luma_cur, qw, qh)
            self.stats["passthrough"] += 1
            return source_tex

        # 3. 运动估计（当前 1/4 luma vs 上一帧 1/4 luma）
        luma_prev = self._surfaces.get("temporal_luma_prev", qw, qh)
        if luma_prev is None:
            return source_tex
        motion = self._estimate_motion(luma_cur.texture, luma_prev.texture, p, qw, qh)
        if motion is None:
            # 运动估计失败：仅做零运动累积（warp 退化为直采）
            self._store_prev_luma(luma_cur, qw, qh)
            return source_tex

        # 4. 时域累积
        accum = self._prog("temporal_accum", vs.TEMPORAL_ACCUM_FRAGMENT)
        if accum is None:
            return source_tex
        blend_scale = _FRAMES_BLEND_SCALE.get(p.frames, 1.0)
        effective_blend = min(1.0, p.blend * blend_scale)
        strength = max(p.luma, p.chroma) / 100.0
        sigma_full = 0.02 + 0.10 * strength

        out.use()
        self._ctx.viewport = (0, 0, width, height)
        source_tex.use(0)
        prev_hist.texture.use(1)
        motion.texture.use(2)
        accum["u_current"].value = 0
        accum["u_history"].value = 1
        accum["u_motion"].value = 2
        accum["u_luma_strength"].value = p.luma / 100.0
        accum["u_chroma_strength"].value = p.chroma / 100.0
        accum["u_threshold"].value = p.threshold
        accum["u_blend"].value = effective_blend
        accum["u_noise_sigma"].value = sigma_full
        # 场景切换阈值：灵敏度越高（→1），越小的亮度差就判定切换
        accum["u_scene_cut_threshold"].value = (
            0.55 - 0.45 * p.scene_cut_sensitivity
        )
        accum["u_history_frames"].value = float(
            min(self.state.accumulated_frames, 12)
        )
        self._quad.draw(accum)

        # 5. 历史推进：next_hist = 输出
        # AUD-1003 决策记录（保留这份全分辨率 f16 拷贝）：
        # 评估过"accum 直接渲染进 next_hist 并返回 next_hist.texture"以省掉
        # 这次拷贝（4K 每帧 ~0.5-1ms + ~66MB 读写）。下游（debug_overlay /
        # spatial / grading）对返回纹理只采样不写入，同帧内无读写冲突；
        # 返回纹理的寿命契约也相同（下一帧即被重写，当前返回 `out` 同样
        # 如此）。未采用的原因：删除该拷贝意味着因果路径不再请求
        # ``temporal_out`` 表面键，而该键是 RenderSurfaceCache 表面键契约
        # 的一部分（tests/color/test_fix_wd3g20_aud.py 的 causal 键集合
        # 断言、以及 _replay_window 的窗口/因果表面图对称性），单方面
        # 移除属于跨契约变更，超出本修复的最小改动边界；如后续要做，
        # 需连同表面键契约测试与窗口重放路径一并调整。
        next_hist.use()
        self._ctx.viewport = (0, 0, width, height)
        copy_prog = self._prog("copy", vs.COPY_FRAGMENT)
        out_tex = out.texture
        if copy_prog is not None:
            out_tex.use(0)
            copy_prog["u_texture"].value = 0
            self._quad.draw(copy_prog)
        self._swap_history()
        self._store_prev_luma(luma_cur, qw, qh)
        self.stats["frames_processed"] += 1
        return out_tex

    def _swap_history(self) -> None:
        self._hist_index = 1 - self._hist_index

    def _store_prev_luma(self, luma_surface: RenderSurface, qw: int, qh: int) -> None:
        copy_prog = self._prog("copy", vs.COPY_FRAGMENT)
        prev = self._surfaces.get("temporal_luma_prev", qw, qh)
        if copy_prog is None or prev is None:
            self._prev_luma_valid = False
            return
        prev.use()
        self._ctx.viewport = (0, 0, qw, qh)
        luma_surface.texture.use(0)
        copy_prog["u_texture"].value = 0
        self._quad.draw(copy_prog)
        self._prev_luma_valid = True

    # ------------------------------------------------------------------
    # 运动估计

    def _downsample_luma(self, source_tex: moderngl.Texture, target_surface: RenderSurface,
                         width: Optional[int] = None, height: Optional[int] = None) -> None:
        """把 source_tex 降采样为 1/4 亮度写入 target_surface。

        AUD-1001: u_texel 必须取**被采样纹理自身**的尺寸。窗口重放模式的
        邻帧可能是降采样上传（与当前帧分辨率不同），用当前帧尺寸作
        u_texel 会让邻帧的 16-tap box UV 足迹错误，cur/prev luma 的等效
        低通滤波不一致，运动估计出现系统性残差。
        width/height 为旧调用签名的兼容参数，已废弃——传入时忽略。
        """
        prog = self._prog("luma_quarter", vs.LUMA_DOWNSAMPLE_QUARTER_FRAGMENT)
        if prog is None:
            raise RuntimeError("luma_quarter shader unavailable")
        qw, qh = target_surface.size
        target_surface.use()
        self._ctx.viewport = (0, 0, qw, qh)
        source_tex.use(0)
        prog["u_texture"].value = 0
        src_w, src_h = source_tex.size
        prog["u_texel"].value = (1.0 / src_w, 1.0 / src_h)
        self._quad.draw(prog)

    def _estimate_motion(self, luma_cur: moderngl.Texture, luma_prev: moderngl.Texture,
                         p: TemporalDenoiseParams, qw: int, qh: int,
                         key_prefix: str = "") -> Optional[RenderSurface]:
        prog = self._prog("motion_match", vs.MOTION_BLOCK_MATCH_FRAGMENT)
        if prog is None:
            return None
        motion_key = key_prefix + "temporal_motion"
        self._last_motion_key = motion_key
        motion = self._surfaces.get(motion_key, qw, qh)
        if motion is None:
            return None
        search = _MOTION_SEARCH_RADIUS.get(p.motion, 3)
        # 噪声底标定（G-P1: 5x5 = 25 tap 支撑）：假设全分辨率噪声 sigma
        # 随强度 0.02→0.12，1/4 降采样（16 tap box）后 sigma/4；
        # 25 tap SAD 期望 ≈ 25*1.13*sigma_q
        strength = max(p.luma, p.chroma) / 100.0
        sigma_full = 0.02 + 0.10 * strength
        match_scale = 25.0 * 1.13 * (sigma_full / 4.0) * 1.5
        motion.use()
        self._ctx.viewport = (0, 0, qw, qh)
        luma_cur.use(0)
        luma_prev.use(1)
        prog["u_current_luma"].value = 0
        prog["u_previous_luma"].value = 1
        prog["u_luma_texel"].value = (1.0 / qw, 1.0 / qh)
        prog["u_search_radius"].value = search
        prog["u_match_scale"].value = max(0.02, match_scale)
        self._quad.draw(prog)

        # G-P1 运动场平滑：收敛逐像素分裂（缓慢平移时相邻像素在两个
        # 整数位置间跳变），置信度取 9 tap 最小值（分裂=不确定 → 降权）。
        smooth_prog = self._prog("motion_smooth", vs.MOTION_SMOOTH_FRAGMENT)
        smoothed = self._surfaces.get(
            key_prefix + "temporal_motion_sm", qw, qh
        )
        if smooth_prog is not None and smoothed is not None:
            smoothed.use()
            self._ctx.viewport = (0, 0, qw, qh)
            motion.texture.use(0)
            smooth_prog["u_motion"].value = 0
            smooth_prog["u_texel"].value = (1.0 / qw, 1.0 / qh)
            self._quad.draw(smooth_prog)
            return smoothed
        return motion

    # ------------------------------------------------------------------
    # 调试叠加（video_denoise_debug_overlay：最近一次运动场可视化）

    def debug_overlay(self, source_tex: moderngl.Texture) -> moderngl.Texture:
        """把最近一次运动估计场叠加到 source_tex 上（调试用）。

        无可用运动场（首帧/运动估计失败）时原样返回。
        """
        width, height = source_tex.size
        motion = self._surfaces.peek(self._last_motion_key)
        if motion is None:
            return source_tex
        prog = self._prog("debug_overlay", vs.DEBUG_OVERLAY_FRAGMENT)
        out = self._surfaces.get("temporal_debug", width, height)
        if prog is None or out is None:
            return source_tex
        try:
            out.use()
            self._ctx.viewport = (0, 0, width, height)
            source_tex.use(0)
            motion.texture.use(1)
            prog["u_texture"].value = 0
            prog["u_motion"].value = 1
            self._quad.draw(prog)
            return out.texture
        except Exception:
            logger.exception("VideoTemporalDenoiser: debug overlay failed")
            return source_tex

    # ------------------------------------------------------------------
    # 窗口重放模式（Paused/Export：给定最旧→最新邻帧纹理序列）

    def process_window(
        self,
        neighbor_textures: Sequence[moderngl.Texture],
        current_tex: moderngl.Texture,
        params: TemporalDenoiseParams,
    ) -> moderngl.Texture:
        """对有序帧序列（最旧→最新，最后一项为当前帧）顺序重放累积。

        邻帧纹理由调用方上传/管理；此模式不使用/不污染因果历史状态。
        """
        p = params.sanitized()
        frames = [t for t in neighbor_textures if t is not None] + [current_tex]
        if not p.is_active() or len(frames) < 2:
            return current_tex

        width, height = current_tex.size
        if width < 16 or height < 16:
            return current_tex

        try:
            return self._replay_window(frames, p, width, height)
        except Exception:
            logger.exception("VideoTemporalDenoiser: window replay failed")
            return current_tex

    def _replay_window(self, frames: Sequence[moderngl.Texture], p: TemporalDenoiseParams,
                       width: int, height: int) -> moderngl.Texture:
        accum = self._prog("temporal_accum", vs.TEMPORAL_ACCUM_FRAGMENT)
        copy_prog = self._prog("copy", vs.COPY_FRAGMENT)
        if accum is None or copy_prog is None:
            return frames[-1]

        qw = max(1, width // 4)
        qh = max(1, height // 4)
        # Window replay is an independent scratch graph.  Reusing causal
        # surfaces makes a paused preview silently become the next playback
        # history, even though TemporalDenoiseState itself was not advanced.
        prefix = "window_"
        luma_cur = self._surfaces.get(prefix + "temporal_luma_cur", qw, qh)
        luma_prev = self._surfaces.get(prefix + "temporal_luma_prev", qw, qh)
        luma_tmp = self._surfaces.get(prefix + "temporal_luma_tmp", qw, qh)
        hist_a = self._surfaces.get(prefix + "temporal_hist_a", width, height)
        hist_b = self._surfaces.get(prefix + "temporal_hist_b", width, height)
        out = self._surfaces.get(prefix + "temporal_out", width, height)
        if any(
            s is None
            for s in (luma_cur, luma_prev, luma_tmp, hist_a, hist_b, out)
        ):
            return frames[-1]

        blend_scale = _FRAMES_BLEND_SCALE.get(p.frames, 1.0)
        effective_blend = min(1.0, p.blend * blend_scale)
        scene_cut_threshold = 0.55 - 0.45 * p.scene_cut_sensitivity
        strength = max(p.luma, p.chroma) / 100.0
        sigma_full = 0.02 + 0.10 * strength
        last_i = len(frames) - 1

        prev_hist = hist_a
        cur_hist = hist_b

        # 初始化：历史 = 最旧帧
        prev_hist.use()
        self._ctx.viewport = (0, 0, width, height)
        frames[0].use(0)
        copy_prog["u_texture"].value = 0
        self._quad.draw(copy_prog)
        self._downsample_luma(frames[0], luma_prev)

        result = frames[-1]
        for i, frame_tex in enumerate(frames[1:], start=1):
            # 当前帧亮度（u_texel 按各帧自身尺寸，见 _downsample_luma）
            self._downsample_luma(frame_tex, luma_cur)
            motion = self._estimate_motion(
                luma_cur.texture, luma_prev.texture, p, qw, qh,
                key_prefix=prefix,
            )
            if motion is None:
                continue

            # G-P1 分层权重：邻帧可能是降采样上传（细节先天偏软），
            # 邻帧轮用半权重累积；最后一轮（当前帧，全分辨率直采）
            # 用全权重——保证当前帧细节占比，避免重放输出整体模糊。
            is_final = (i == last_i)
            pass_blend = effective_blend if is_final else effective_blend * 0.5

            out.use()
            self._ctx.viewport = (0, 0, width, height)
            frame_tex.use(0)
            prev_hist.texture.use(1)
            motion.texture.use(2)
            accum["u_current"].value = 0
            accum["u_history"].value = 1
            accum["u_motion"].value = 2
            accum["u_luma_strength"].value = p.luma / 100.0
            accum["u_chroma_strength"].value = p.chroma / 100.0
            accum["u_threshold"].value = p.threshold
            accum["u_blend"].value = pass_blend
            accum["u_noise_sigma"].value = sigma_full
            accum["u_scene_cut_threshold"].value = scene_cut_threshold
            accum["u_history_frames"].value = float(min(i + 1, 12))
            self._quad.draw(accum)

            # cur_hist = out；交换
            cur_hist.use()
            self._ctx.viewport = (0, 0, width, height)
            out.texture.use(0)
            copy_prog["u_texture"].value = 0
            self._quad.draw(copy_prog)
            prev_hist, cur_hist = cur_hist, prev_hist

            # luma 推进
            luma_tmp.use()
            self._ctx.viewport = (0, 0, qw, qh)
            luma_cur.texture.use(0)
            copy_prog["u_texture"].value = 0
            self._quad.draw(copy_prog)
            luma_prev, luma_cur = luma_cur, luma_tmp

            result = out.texture

        self.stats["frames_processed"] += 1
        return result

    # ------------------------------------------------------------------

    def release(self) -> None:
        for prog in self._programs.values():
            try:
                prog.release()
            except Exception:
                pass
        self._programs.clear()
