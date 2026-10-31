"""T-P1-22：Adjustment Clip 交付完整性聚焦测试。

任务卡验证要求：
1. 像素级场景——底轨纯色 + 上层 Adjustment Clip（可量化 exposure /
   blur），导出后作用范围内/外像素可区分：范围内均值偏离基色、范围外
   等于基色（作用域只含下方轨道且按时区分段）。
2. 导出日志不得出现 ``Unsupported asset type: adjustment``（adjustment
   无源帧是契约而非缺失）。
3. plan compiler：SET_TRANSFORM / SET_CROP / SET_SPEED 的 JSON 值编译为
   命令层可应用的 dataclass 实例（此前 param_type 映射缺失 + dict 不
   落地，编译通过但 apply 必炸）。
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from src.features.edit.ai_editing.models import OperationKind
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportSource,
    TimelineExportConfig,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from src.shared.project.color_stack import ColorStackData

REPO_ROOT = None  # 使用相对 repo 的测试素材
TOPOS_SOURCE_NAME = "2K_topos_422_Proxy.mov"  # C02 冻结语料规范名


def _build_project(width: int = 320, height: int = 180):
    return EditProject.create(name="tp122-adjustment"), width, height


def _timeline(adjustment_effects=(), color_stack_id=None,
              frames: int = 8, width: int = 320, height: int = 180,
              base_rgb=(64.0, 128.0, 192.0)):
    project = EditProject.create(name="tp122-adjustment")
    fps = Rational(25, 1)
    asset = MediaAsset(
        asset_id="base_solid", path="", media_type="solid",
        display_name="base",
        metadata={"color": base_rgb, "fps": 25.0, "total_frames": frames},
    )
    project = project.with_media_asset_added(asset)
    solid = ClipInstance(
        clip_id="base_clip", asset_id="base_solid", track_id="v1",
        timeline_start=0, timeline_duration=frames, source_start=0,
        source_duration=frames,
    )
    adj_asset = MediaAsset(
        asset_id="adj", path="", media_type="adjustment",
        display_name="adj", metadata={"fps": 25.0},
    )
    project = project.with_media_asset_added(adj_asset)
    adj = ClipInstance(
        clip_id="adj_clip", asset_id="adj", track_id="v2",
        timeline_start=frames // 2, timeline_duration=frames // 2,
        source_start=0, source_duration=frames // 2,
        effects=tuple(adjustment_effects),
        color_stack_id=color_stack_id,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(solid,))
    v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                   clips=(adj,))
    seq = TimelineSequence(timeline_id="s", name="AdjTest", fps=fps,
                           width=width, height=height,
                           video_tracks=(v2, v1))
    return project, seq


def _export(project, seq, tmp_path, frames: int = 8):
    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id="s",
        output_path=str(tmp_path / "adj_out.mov"),
        codec=ExportCodec.H264, pixel_format="yuv420p",
        quality=20_000_000, quality_mode="vbr",
        fps=Rational(25, 1), include_audio=False, burn_subtitles=False,
    )
    exporter = create_timeline_exporter(project, seq)
    outcome = exporter.export_timeline(cfg)
    return outcome, tmp_path / "adj_out.mov"


def _best_effort_unlink(path) -> None:
    """W07：Windows 下 av/工厂缓存可能滞后持有句柄（WinError 32）——
    gc+短重试后仍锁则留给 pytest 临时目录清理（删除是测试卫生，不是断言）。"""
    import gc
    import time
    from pathlib import Path as _P

    target = _P(path)
    for _ in range(6):
        try:
            target.unlink(missing_ok=True)
            return
        except PermissionError:
            gc.collect()
            time.sleep(0.05)


def _frame_rgb_mean(path, frame_index, width=320, height=180):
    import gc

    from src.shared.media.factory import MediaSourceFactory

    handle = MediaSourceFactory.create(str(path))
    handle.open()
    try:
        frame = handle.read_frame(frame_index)
        data = getattr(frame, "data", None)
        if data is None:
            planes = frame.planes
            import numpy as np
            result = float(np.asarray(planes[0]).mean())
        else:
            import numpy as np
            result = float(np.asarray(data).mean())
        return result
    finally:
        handle.close()
        # W07：解出的 av 帧对象持有容器引用——close() 后若仍存活，
        # Windows 保持文件句柄（WinError 32），调用方的 unlink 失败。
        # POSIX 允许 unlink 打开中的文件，此问题只在 Windows 显形。
        del frame
        gc.collect()


class TestAdjustmentPixelScope:
    def test_unsupported_asset_type_warning_absent(
            self, tmp_path, caplog):
        """导出含 adjustment 层的时间线不得出现误导性 warning。"""
        project, seq = _timeline()
        with caplog.at_level(logging.WARNING):
            outcome, out = _export(project, seq, tmp_path)
        assert bool(outcome), f"export failed: {getattr(outcome, 'cause', '')}"
        assert out.exists()
        warnings = [r.getMessage() for r in caplog.records]
        assert not any("Unsupported asset type: adjustment" in w
                       for w in warnings), warnings
        _best_effort_unlink(out)

    def test_colorstack_on_adjustment_affects_lower_track(
            self, tmp_path):
        """非空 ColorStack（exposure）挂在 adjustment 上必须影响下方轨道。

        纯色底 + exposure±：作用范围内均值显著偏离基色，范围外等于基色。
        """
        project, seq = _timeline(color_stack_id="adj_stack")
        project.color_stacks["adj_stack"] = ColorStackData(
            stack_id="adj_stack", asset_id="adj",
            grading_params={"exposure": 80.0},
        )
        outcome, out = _export(project, seq, tmp_path)
        assert bool(outcome), f"export failed: {getattr(outcome, 'cause', '')}"
        inside = _frame_rgb_mean(out, 6)   # adjustment 作用范围内
        outside = _frame_rgb_mean(out, 1)  # 作用范围外
        _best_effort_unlink(out)
        # exposure 80 → ~+3.8 stops（the_decompiler 曲线），8-bit 均值
        # 应显著抬升（纯色 128 基 → 接近饱和），范围外保持基色。
        assert inside - outside > 20.0, (
            f"ColorStack on adjustment clip 被静默丢弃: "
            f"inside={inside:.2f} outside={outside:.2f}")


class TestPlanCompilerClipNativeParams:
    def _compile(self, kind, params):
        from src.features.edit.ai_editing.models import (
            EditOperation, OperationKind, PlanTarget, EditPlan,
        )

        plan = EditPlan.create(PlanTarget("p", "t"), 0, operations=(
            EditOperation("op1", kind, params=params),))
        from src.features.edit.ai_editing.plan_compiler import compile_plan

        compiled = compile_plan(plan)
        # MacroCommand 聚合所有子命令
        return list(getattr(compiled.macro, "sub_commands", []) or [])

    def test_set_transform_dict_compiles_to_dataclass(self) -> None:
        from src.features.edit.core.clip_params import TransformParams

        commands = self._compile(OperationKind.SET_TRANSFORM, {
            "clip_id": "c1", "track_id": "v1",
            "transform": {"translate_x": 10.0, "scale_x": 1.5},
        })
        cmd = commands[-1]
        assert cmd.param_type == "transform"
        assert isinstance(cmd.new_value, TransformParams)
        assert cmd.new_value.translate_x == 10.0

    def test_set_crop_dict_compiles_to_dataclass(self) -> None:
        from src.features.edit.core.clip_params import CropParams

        commands = self._compile(OperationKind.SET_CROP, {
            "clip_id": "c1", "track_id": "v1",
            "crop": {"left": 0.1, "right": 0.1},
        })
        cmd = commands[-1]
        assert cmd.param_type == "crop"
        assert isinstance(cmd.new_value, CropParams)

    def test_set_speed_numeric_becomes_speed_params(self) -> None:
        from src.features.edit.core.clip_params import SpeedParams

        commands = self._compile(OperationKind.SET_SPEED, {
            "clip_id": "c1", "track_id": "v1", "speed": 2.0,
        })
        cmd = commands[-1]
        assert isinstance(cmd.new_value, SpeedParams)
        assert cmd.new_value.speed == 2.0

    def test_invalid_transform_payload_fails_at_compile(self) -> None:
        from src.features.edit.ai_editing.plan_compiler import CompileError

        with pytest.raises(Exception):
            self._compile(OperationKind.SET_TRANSFORM, {
                "clip_id": "c1", "track_id": "v1",
                "transform": "not-a-dict",
            })


class TestSeekLoopStability:
    """T-P1-22：H265/ProRes 循环 seek 20 次无错误、无 leftover 泄漏。"""

    @pytest.mark.parametrize("video_name", ["2K_H265.mov", "2K_prores422HQ.mov"])
    def test_twenty_seek_cycles_stable(self, video_name):
        from src.features.edit.core.audio_playback import AudioDecoder

        path = __import__("pathlib").Path(__file__).resolve().parents[2] \
            / "tests" / "test_videos" / video_name
        if not path.is_file():
            pytest.skip("素材缺失")
        d = AudioDecoder()
        assert d.open(path, timeline_fps=25.0, source_fps=25.0,
                      output_sample_rate=48000)
        try:
            import numpy as np

            first_chunks = []
            for cycle in range(20):
                assert d.seek_to_time(0.0) is True
                assert d._sample_remainder is None, (
                    f"cycle {cycle}: leftover survived seek")
                chunk = d.decode_next_samples(1920)
                assert chunk is not None
                assert np.all(np.isfinite(chunk))
                if cycle == 0:
                    first_chunks.append(chunk.copy())
                else:
                    # 每次 seek 回 0 后的首块必须逐位一致（无状态残留）
                    assert np.array_equal(first_chunks[0], chunk), (
                        f"cycle {cycle}: decode after seek differs")
        finally:
            d.close()
