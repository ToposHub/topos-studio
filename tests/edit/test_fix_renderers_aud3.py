"""批 A（AUD-18500/18600/18801）根因修复回归测试。

- AUD-18801：slide_clip 在紧邻轨道上任何方向的 slide 都因中间态触发
  with_clip_updated 的重叠校验抛裸 ValueError。修复后必须返回合法
  最终态（原子 _rebuild），非法 delta 仍抛 TimelineOperationError。
- AUD-18600：render_layer_stack 同帧多个转场复用同名 CPU 上传缓冲
  （stack_wipe_params / stack_wipe_mask_params / stack_wipe_mask），
  第 2+ 个转场的 write_buffer 在整帧 command buffer 提交前覆盖第 1 个
  转场已编码 dispatch 的参数。用录制型 Fake Metal context（dispatch
  只编码、finish 才提交，镜像真实时序）断言每个转场绑定独立缓冲且
  提交时刻内容各自正确。
- AUD-18500：deep-u16 效果桥代码块缩进脱出 if deep_u16:——deep 无
  grade 时效果链被静默丢弃；8-bit planar 源带效果链时对未分配的
  _tex_mid_u16[0] 执行桥导致整帧 None。修复后两条路径都必须产出
  含效果的像素。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.features.edit.core.timeline_metal_export_renderer import (  # noqa: E402
    _WIPE_BLEND_PARAMS_DTYPE,
    _TRANSITION_MASK_PARAMS_DTYPE,
)
from src.features.edit.core.timeline_ops import (  # noqa: E402
    TimelineOperationError,
    slide_clip,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational  # noqa: E402


# ======================================================================
# AUD-18801：slide_clip 中间态重叠校验
# ======================================================================

def _clip(clip_id: str, start: int, duration: int, source_start: int) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset1",
        track_id="track1",
        timeline_start=start,
        timeline_duration=duration,
        source_start=source_start,
        source_duration=duration,
        speed=1.0,
    )


def _sequence_with(clips: tuple[ClipInstance, ...]) -> TimelineSequence:
    track = EditTrack(
        track_id="track1",
        name="Video 1",
        track_type="video",
        index=0,
        clips=clips,
    )
    return TimelineSequence(
        timeline_id="seq1",
        name="Slide Test",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(track,),
    )


@pytest.fixture
def adjacent_sequence() -> TimelineSequence:
    """A[0,100] B[100,200] C[200,300]——紧邻布局，slide 的典型场景。"""
    return _sequence_with((
        _clip("A", 0, 100, 0),
        _clip("B", 100, 100, 100),
        _clip("C", 200, 100, 200),
    ))


def _layout(sequence: TimelineSequence) -> dict[str, tuple[int, int, int, int]]:
    track = sequence.get_track_by_id("track1")
    assert track is not None
    return {
        c.clip_id: (c.timeline_start, c.timeline_end,
                    c.source_start, c.source_duration)
        for c in track.clips
    }


def _assert_no_overlap(sequence: TimelineSequence) -> None:
    layout = _layout(sequence)
    ordered = sorted(layout.items(), key=lambda kv: kv[1][0])
    for (_, (s1, e1, _, _)), (_, (s2, e2, _, _)) in zip(ordered, ordered[1:]):
        assert e1 <= s2, f"重叠: {e1} > {s2}"


def test_slide_adjacent_positive_delta_returns_final_state(
        adjacent_sequence: TimelineSequence) -> None:
    """修复前：slide +30 → ValueError: Updated clip overlaps with C。"""
    result = slide_clip(adjacent_sequence, "B", 30)
    layout = _layout(result)
    # A 延长补位、B 平移、C 起点右移且 source 随之调整
    assert layout["A"] == (0, 130, 0, 130)
    assert layout["B"] == (130, 230, 100, 100)
    assert layout["C"] == (230, 300, 230, 70)
    _assert_no_overlap(result)


def test_slide_adjacent_negative_delta_returns_final_state(
        adjacent_sequence: TimelineSequence) -> None:
    """修复前：slide -30 → ValueError: Updated clip overlaps with A。"""
    result = slide_clip(adjacent_sequence, "B", -30)
    layout = _layout(result)
    assert layout["A"] == (0, 70, 0, 70)
    assert layout["B"] == (70, 170, 100, 100)
    assert layout["C"] == (170, 300, 170, 130)
    _assert_no_overlap(result)


@pytest.mark.parametrize("delta", [-60, -1, 1, 30, 60, 99])
def test_slide_adjacent_matrix_all_deltas_succeed(
        adjacent_sequence: TimelineSequence, delta: int) -> None:
    """紧邻布局下任意合法 delta 都不抛中间态异常且最终态无重叠。"""
    result = slide_clip(adjacent_sequence, "B", delta)
    _assert_no_overlap(result)
    layout = _layout(result)
    assert layout["B"][0] == 100 + delta
    # gap 总量守恒：A_start 与 C_end 不变
    assert layout["A"][0] == 0
    assert layout["C"][1] == 300
    # 邻接关系保持：A 末缘 == B 新起点，B 新末缘 == C 新起点
    assert layout["A"][1] == layout["B"][0]
    assert layout["B"][1] == layout["C"][0]


def test_slide_gapped_neighbors_both_directions() -> None:
    """带间隙布局：两侧 gap 分别守恒，不能被邻居吞掉。"""
    sequence = _sequence_with((
        _clip("A", 0, 50, 0),
        _clip("B", 100, 100, 100),
        _clip("C", 250, 50, 200),
    ))
    right = slide_clip(sequence, "B", 40)
    layout = _layout(right)
    assert layout["A"] == (0, 90, 0, 90)
    assert layout["B"] == (140, 240, 100, 100)
    assert layout["C"] == (290, 300, 240, 10)
    assert layout["B"][0] - layout["A"][1] == 50
    assert layout["C"][0] - layout["B"][1] == 50
    _assert_no_overlap(right)

    left = slide_clip(sequence, "B", -40)
    layout = _layout(left)
    assert layout["A"] == (0, 10, 0, 10)
    assert layout["B"] == (60, 160, 100, 100)
    assert layout["C"] == (210, 300, 160, 90)
    assert layout["B"][0] - layout["A"][1] == 50
    assert layout["C"][0] - layout["B"][1] == 50
    _assert_no_overlap(left)


def test_slide_invalid_delta_raises_typed_error(
        adjacent_sequence: TimelineSequence) -> None:
    """越界 delta 仍按契约抛 TimelineOperationError（而非模型层 ValueError）。"""
    with pytest.raises(TimelineOperationError):
        slide_clip(adjacent_sequence, "B", 150)  # C 时长将被吃穿
    with pytest.raises(TimelineOperationError):
        slide_clip(adjacent_sequence, "B", -200)  # 起点移到 0 之前


# ======================================================================
# AUD-18600：同帧多转场的 CPU 上传缓冲竞争（Fake Metal context）
# ======================================================================

class _FakeMetalBuffer:
    """Shared 存储 MTLBuffer 替身：CPU 可直接 memcpy contents。"""

    def __init__(self, size: int) -> None:
        self._size = int(size)
        self.contents = bytearray(self._size)

    def length(self) -> int:
        return self._size


class _RecordingMetalContext:
    """镜像真实时序：dispatch 只编码、finish 才提交；write_buffer 立即 memcpy。"""

    def __init__(self) -> None:
        self.last_gpu_time_ms: float = 0.0
        self.dispatches: list[tuple[str, dict[int, Any]]] = []
        self.finish_count = 0

    def compile_kernel(self, name: str, _source: str) -> str:
        return name

    def create_buffer(self, size: int) -> _FakeMetalBuffer:
        return _FakeMetalBuffer(size)

    def create_private_buffer(self, size: int) -> _FakeMetalBuffer:
        return _FakeMetalBuffer(size)

    def write_buffer(self, buffer: _FakeMetalBuffer, data: np.ndarray,
                     *, offset: int = 0) -> None:
        payload = np.ascontiguousarray(data).view(np.uint8).reshape(-1)
        buffer.contents[offset:offset + payload.nbytes] = payload.tobytes()

    def dispatch(self, kernel: str, *, buffers: dict[int, Any] | None = None,
                 textures: dict[int, Any] | None = None,
                 threads: tuple[int, int, int] = (1, 1, 1),
                 group: tuple[int, int, int] | None = None) -> None:
        self.dispatches.append((str(kernel), dict(buffers or {})))

    def finish(self) -> None:
        self.finish_count += 1

    def read_buffer(self, buffer: _FakeMetalBuffer,
                    size: int | None = None) -> np.ndarray:
        length = buffer.length() if size is None else int(size)
        return np.frombuffer(
            bytes(buffer.contents[:length]), dtype=np.uint8)


def _make_wipe(left: np.ndarray, right: np.ndarray, opacity: float,
               mask: np.ndarray | None = None,
               mask_spec: Any = None) -> Any:
    from src.features.edit.core.timeline_gpu_export_renderer import GpuWipeElement

    return GpuWipeElement(
        left=left, left_grade=None, right=right, right_grade=None,
        mask=mask, left_opacity=1.0, right_opacity=1.0, opacity=opacity,
        mask_spec=mask_spec,
    )


def _run_two_wipe_stack(monkeypatch: pytest.MonkeyPatch, wipes: list[Any],
                        ) -> tuple[Any, _RecordingMetalContext, set[str]]:
    import src.features.edit.core.timeline_metal_export_renderer as metal_mod

    context = _RecordingMetalContext()
    monkeypatch.setattr(metal_mod, "MetalComputeContext", lambda: context)
    renderer = metal_mod.TimelineMetalExportRenderer()
    try:
        result = renderer.render_layer_stack(list(wipes), 96, 64)
        buffer_names = set(renderer._buffers)
    finally:
        renderer.release()
    return result, context, buffer_names


def _params_view(buffer: _FakeMetalBuffer) -> np.ndarray:
    return np.frombuffer(
        bytes(buffer.contents[:_WIPE_BLEND_PARAMS_DTYPE.itemsize]),
        dtype=_WIPE_BLEND_PARAMS_DTYPE,
    )


def test_metal_two_wipes_use_distinct_param_and_mask_buffers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """CPU 掩码路径：两个转场的 wipe_params / wipe_mask 缓冲必须独立，
    提交时刻（finish 后）内容仍各自正确（修复前同名缓冲被第 2 个转场
    复写，第 1 个转场读到串台参数）。"""
    frame_a = np.full((64, 96, 3), 40, dtype=np.uint8)
    frame_b = np.full((64, 96, 3), 210, dtype=np.uint8)
    frame_c = np.full((64, 96, 3), 90, dtype=np.uint8)
    frame_d = np.full((64, 96, 3), 160, dtype=np.uint8)
    mask = np.full((64, 96), 0.5, dtype=np.float32)

    result, context, buffer_names = _run_two_wipe_stack(monkeypatch, [
        _make_wipe(frame_a, frame_b, opacity=0.11, mask=mask),
        _make_wipe(frame_c, frame_d, opacity=0.77, mask=mask),
    ])
    assert result is not None

    composites = [d for d in context.dispatches
                  if d[0] == "timeline_wipe_composite"]
    assert len(composites) == 2

    first_params = composites[0][1][5]
    second_params = composites[1][1][5]
    # 核心断言 1：dispatch 绑定的参数缓冲对象互不相同（否则第 2 次上传
    # 必然覆盖第 1 个转场已编码 dispatch 的读源）
    assert first_params is not second_params
    # 核心断言 2：整帧提交时刻，两块缓冲内容各自正确
    assert float(_params_view(first_params)["control"][0][0]) == pytest.approx(0.11)
    assert float(_params_view(second_params)["control"][0][0]) == pytest.approx(0.77)

    first_mask = composites[0][1][3]
    second_mask = composites[1][1][3]
    assert first_mask is not second_mask
    # 旧的同名全局键不复存在；新键按转场 layer 索引分桶并挂进
    # stack_layer_{i}_ 剪枝域（plan 缩短时自动释放）
    assert "stack_wipe_params" not in buffer_names
    assert "stack_wipe_mask" not in buffer_names
    assert "stack_wipe_mask_params" not in buffer_names


def test_metal_two_wipes_use_distinct_procedural_mask_param_buffers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """mask_spec（程序化掩码）路径：transition_mask 的参数缓冲同样独立。"""
    frame_a = np.full((64, 96, 3), 40, dtype=np.uint8)
    frame_b = np.full((64, 96, 3), 210, dtype=np.uint8)
    frame_c = np.full((64, 96, 3), 90, dtype=np.uint8)
    frame_d = np.full((64, 96, 3), 160, dtype=np.uint8)

    result, context, buffer_names = _run_two_wipe_stack(monkeypatch, [
        _make_wipe(frame_a, frame_b, opacity=0.2,
                   mask_spec=SimpleNamespace(progress=0.25)),
        _make_wipe(frame_c, frame_d, opacity=0.8,
                   mask_spec=SimpleNamespace(progress=0.75)),
    ])
    assert result is not None

    masks = [d for d in context.dispatches
             if d[0] == "timeline_transition_mask"]
    assert len(masks) == 2
    first_buf = masks[0][1][1]
    second_buf = masks[1][1][1]
    assert first_buf is not second_buf
    first = np.frombuffer(
        bytes(first_buf.contents[:_TRANSITION_MASK_PARAMS_DTYPE.itemsize]),
        dtype=_TRANSITION_MASK_PARAMS_DTYPE)
    second = np.frombuffer(
        bytes(second_buf.contents[:_TRANSITION_MASK_PARAMS_DTYPE.itemsize]),
        dtype=_TRANSITION_MASK_PARAMS_DTYPE)
    assert float(first["control"][0][0]) == pytest.approx(0.25)
    assert float(second["control"][0][0]) == pytest.approx(0.75)
    assert "stack_wipe_mask_params" not in buffer_names


# ======================================================================
# AUD-18500：deep-u16 桥缩进（真实 GL；headless 无 GL 自动 skip）
# ======================================================================

def _gl_available() -> bool:
    try:
        import moderngl

        ctx = moderngl.create_standalone_context(require=330)
        ctx.release()
        return True
    except Exception:
        return False


def _make_planes(fmt: str, w: int = 96, h: int = 64):
    rng = np.random.default_rng(11)
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
    xg, yg = np.meshgrid(xs, ys)
    base = (0.5 + 0.25 * np.sin(9 * xg * 3.14159) * np.cos(7 * yg * 3.14159)
            + 0.06 * rng.standard_normal((h, w)).astype(np.float32))
    u_base = 0.5 + 0.2 * np.sin(5 * xg * 3.14159 + 1.0)
    v_base = 0.5 + 0.2 * np.cos(3 * yg * 3.14159 + 0.5)
    bits = 10 if "10le" in fmt else 8
    dtype = np.uint16 if bits == 10 else np.uint8
    scale8 = 1 << max(0, bits - 8)
    y = np.rint(np.clip(base, 0.0, 1.0) * 219.0 * scale8
                + 16.0 * scale8).astype(dtype)
    u_small = u_base[::2, ::2] if "420" in fmt else (
        u_base[:, ::2] if "422" in fmt else u_base)
    v_small = v_base[::2, ::2] if "420" in fmt else (
        v_base[:, ::2] if "422" in fmt else v_base)
    u = np.rint(np.clip(u_small, 0.0, 1.0) * 224.0 * scale8
                + 128.0 * scale8).astype(dtype)
    v = np.rint(np.clip(v_small, 0.0, 1.0) * 224.0 * scale8
                + 128.0 * scale8).astype(dtype)
    return (np.ascontiguousarray(y), np.ascontiguousarray(u),
            np.ascontiguousarray(v))


def _blur_chain() -> tuple[tuple[Any, dict[str, Any]], ...]:
    from src.features.shared.effects.effect_catalog import get_effect_catalog

    spec = get_effect_catalog().get_spec("blur")
    assert spec is not None
    return ((spec, {
        "blur_type": "gaussian", "radius": 12.0, "mix": 1.0,
        "edge_policy": "clamp",
        "channel_r": True, "channel_g": True, "channel_b": True,
        "channel_a": False,
    }),)


def _render_stack_with_planar(
        output_u16: bool, chain: tuple[tuple[Any, dict[str, Any]], ...] | None,
) -> tuple[Any, Any]:
    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource, TimelineGpuExportRenderer,
    )

    planes = _make_planes("yuv422p10le")
    src = GpuYuvSource(planes=planes, luma_bits=10, chroma_x_div=2,
                       chroma_y_div=1, full_range=False, kr=0.2126,
                       kb=0.0722)
    renderer = TimelineGpuExportRenderer()
    try:
        element = (src, None, 1.0, 0, chain)
        rendered = renderer.render_layer_stack(
            [element], 192, 128, output_u16=output_u16)
        return rendered, renderer
    except Exception:
        renderer.release()
        raise


def _fraction_changed(a: np.ndarray, b: np.ndarray, threshold: int) -> float:
    diff = np.abs(a.astype(np.int32) - b.astype(np.int32))
    return float((diff > threshold).mean())


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_u16_planar_no_grade_effect_chain_applies_effects() -> None:
    """deep_u16 + 无 grade + 效果链：修复前 1617 行提前 return 丢弃效果链
    （输出与无链渲染逐位一致）；修复后经 u16→f2→EPR→u16 桥产出含效果
    的 u16 帧。"""
    with_chain, renderer = _render_stack_with_planar(True, _blur_chain())
    try:
        assert with_chain is not None, "deep-u16 桥渲染失败（整帧回落 CPU）"
        assert with_chain.dtype == np.uint16
        assert with_chain.shape == (128, 192, 3)
        without_chain, renderer2 = _render_stack_with_planar(True, None)
        try:
            assert without_chain is not None
            # 高斯 blur 半径 12 显著改变中频图样；阈值取 u16 域 1% LSB
            assert _fraction_changed(with_chain, without_chain, 257) > 0.02, (
                "效果链未生效：输出与无效果链渲染一致（AUD-18500 回归）")
        finally:
            renderer2.release()
    finally:
        renderer.release()


@pytest.mark.skipif(not _gl_available(), reason="headless GL 不可用")
def test_gpu_u8_planar_effect_chain_uses_f1_path() -> None:
    """deep_u16=False + planar 源 + 效果链：修复前对未分配的
    _tex_mid_u16[0] 执行 deep 桥 → None（整帧回落 CPU）；修复后走
    f1 效果链路径产出含效果的 u8 帧。"""
    with_chain, renderer = _render_stack_with_planar(False, _blur_chain())
    try:
        assert with_chain is not None, (
            "8-bit planar 效果链渲染失败——GPU 路径被误杀（AUD-18500 回归）")
        assert with_chain.dtype == np.uint8
        assert with_chain.shape == (128, 192, 3)
        without_chain, renderer2 = _render_stack_with_planar(False, None)
        try:
            assert without_chain is not None
            assert _fraction_changed(with_chain, without_chain, 1) > 0.02, (
                "效果链未生效：输出与无效果链渲染一致")
        finally:
            renderer2.release()
    finally:
        renderer.release()
