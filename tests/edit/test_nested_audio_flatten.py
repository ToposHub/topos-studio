"""P1-02 嵌套序列音频展开测试（父时间线混音）。

纯函数 flatten_nested_audio_tracks：
- 子序列音频轨平移进父坐标（parent = child + (nested.start - nested.source_start)）
- 嵌套片段源窗口钳制（窗口外内容不发声）
- 轨参数 / 自动化关键帧平移 / Bus 拓扑命名空间重映射
- 多实例独立、孙辈递归、环与速度≠1x 安全跳过

消费方接线：
- 导出 expand_sequence_audio_for_render（timeline_export）
- 实时 EditPage._sync_audio_tracks_from_timeline（resolver 注入后虚拟轨进引擎）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.nested_sequence import (
    create_nested_sequence_asset,
    flatten_nested_audio_tracks,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    TimelineSequence,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


def _video_asset(aid: str) -> MediaAsset:
    return MediaAsset(
        asset_id=aid, path=f"/fake/{aid}.mp4", media_type="video",
        display_name=aid, metadata={"fps": 24.0, "total_frames": 600},
    )


def _a_clip(clip_id: str, asset_id: str, track_id: str,
            start: int, duration: int) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=asset_id, track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=start, source_duration=duration,
    )


def _child_sequence(child_id: str, clips=(), bus_tracks=()) -> TimelineSequence:
    a1 = EditTrack(
        track_id="ca1", name="ChildA", track_type="audio", index=0,
        clips=tuple(clips),
    )
    return TimelineSequence(
        timeline_id=child_id, name=f"child-{child_id}",
        audio_tracks=(a1, *bus_tracks),
    )


def _parent_with_nested(nested_asset, nested_clip) -> TimelineSequence:
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(nested_clip,))
    a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0)
    return TimelineSequence(
        timeline_id="parent", name="P", video_tracks=(v1,), audio_tracks=(a1,),
    )


def _nested_clip(asset, start=100, duration=50, source_start=0) -> ClipInstance:
    return ClipInstance(
        clip_id="nest1", asset_id=asset.asset_id, track_id="v1",
        timeline_start=start, timeline_duration=duration,
        source_start=source_start, source_duration=duration,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 纯展开
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestFlattenBasic:

    def _setup(self):
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", start=10, duration=30),
        ])
        asset = create_nested_sequence_asset(child, display_name="Nest")
        parent = _parent_with_nested(asset, _nested_clip(asset))
        project = EditProject.create(name="p")
        for a in (asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        resolver = {"child1": child}.get
        return parent, project, resolver

    def test_child_audio_mapped_into_parent(self):
        parent, project, resolver = self._setup()
        tracks = flatten_nested_audio_tracks(parent, resolver, project.assets.get)
        # 自身 1 轨 + 虚拟 1 轨
        assert [t.track_id for t in tracks][:1] == ["a1"]
        virtual = tracks[1]
        assert virtual.track_id == "nest:nest1:ca1"
        # 子 clip start=10 → 父坐标 100+10=110（嵌套片段 start 100, source 0）
        (vc,) = virtual.clips
        assert vc.timeline_start == 110
        assert vc.timeline_duration == 30
        assert vc.source_start == 10
        assert vc.asset_id == "sfx"  # 指向真实素材

    def test_no_resolver_returns_own_tracks(self):
        parent, project, _ = self._setup()
        tracks = flatten_nested_audio_tracks(parent, None, project.assets.get)
        assert [t.track_id for t in tracks] == ["a1"]

    def test_track_params_carried(self):
        from dataclasses import replace
        parent, project, resolver = self._setup()
        loud = replace(parent.audio_tracks[0], volume=0.0)  # 自身轨参数不影响虚拟轨
        parent = parent._rebuild(audio_tracks=(loud,))
        tracks = flatten_nested_audio_tracks(parent, resolver, project.assets.get)
        virtual = tracks[1]
        # 子轨默认参数（volume=1.0）带到虚拟轨
        assert virtual.volume == 1.0


@pytest.mark.edit_core
class TestFlattenWindowClamp:

    def _setup(self, nested_start, nested_duration, nested_source_start,
               child_clip_start, child_clip_duration):
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", child_clip_start, child_clip_duration),
        ])
        asset = create_nested_sequence_asset(child)
        nested = ClipInstance(
            clip_id="nest1", asset_id=asset.asset_id, track_id="v1",
            timeline_start=nested_start, timeline_duration=nested_duration,
            source_start=nested_source_start, source_duration=nested_duration,
        )
        parent = _parent_with_nested(asset, nested)
        project = EditProject.create(name="p")
        for a in (asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        return parent, project, {"child1": child}.get

    def test_clip_beyond_window_is_trimmed(self):
        # 窗口 [0,50)；子 clip [30,80) → 只保留 [30,50)，时长 20
        parent, project, resolver = self._setup(
            nested_start=100, nested_duration=50, nested_source_start=0,
            child_clip_start=30, child_clip_duration=50)
        tracks = flatten_nested_audio_tracks(parent, resolver, project.assets.get)
        (vc,) = tracks[1].clips
        assert vc.timeline_start == 130
        assert vc.timeline_duration == 20
        assert vc.source_start == 30  # 头部未裁，源起点不变

    def test_clip_head_clipped_by_window(self):
        # 窗口 [20,60)；子 clip [0,40) → 保留 [20,40)：
        # 父帧 = nested.start(100) + (20 - source_start(20)) = 100，时长 20，
        # 源起点 +20（头 20 帧被窗口裁掉）
        parent, project, resolver = self._setup(
            nested_start=100, nested_duration=40, nested_source_start=20,
            child_clip_start=0, child_clip_duration=40)
        tracks = flatten_nested_audio_tracks(parent, resolver, project.assets.get)
        (vc,) = tracks[1].clips
        assert vc.timeline_start == 100
        assert vc.timeline_duration == 20
        assert vc.source_start == 20

    def test_clip_fully_outside_window_dropped(self):
        parent, project, resolver = self._setup(
            nested_start=100, nested_duration=50, nested_source_start=0,
            child_clip_start=60, child_clip_duration=30)
        tracks = flatten_nested_audio_tracks(parent, resolver, project.assets.get)
        assert [t.track_id for t in tracks] == ["a1"]  # 无虚拟轨


@pytest.mark.edit_core
class TestFlattenAdvanced:

    def test_speed_not_one_skips_audio(self):
        from src.features.edit.core.clip_params import SpeedParams
        from dataclasses import replace
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", 0, 30)])
        asset = create_nested_sequence_asset(child)
        nested = replace(_nested_clip(asset),
                         speed=SpeedParams(speed=2.0))
        parent = _parent_with_nested(asset, nested)
        project = EditProject.create(name="p")
        for a in (asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        tracks = flatten_nested_audio_tracks(parent, {"child1": child}.get,
                                             project.assets.get)
        assert [t.track_id for t in tracks] == ["a1"]

    def test_two_instances_have_independent_virtual_tracks(self):
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", 0, 30)])
        asset = create_nested_sequence_asset(child)
        n1 = ClipInstance(clip_id="nest1", asset_id=asset.asset_id, track_id="v1",
                          timeline_start=0, timeline_duration=30,
                          source_start=0, source_duration=30)
        n2 = ClipInstance(clip_id="nest2", asset_id=asset.asset_id, track_id="v1",
                          timeline_start=100, timeline_duration=30,
                          source_start=0, source_duration=30)
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(n1, n2))
        parent = TimelineSequence(timeline_id="parent", name="P",
                                  video_tracks=(v1,), audio_tracks=())
        project = EditProject.create(name="p")
        for a in (asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        tracks = flatten_nested_audio_tracks(parent, {"child1": child}.get,
                                             project.assets.get)
        assert len(tracks) == 2
        assert tracks[0].track_id == "nest:nest1:ca1"
        assert tracks[1].track_id == "nest:nest2:ca1"
        assert tracks[0].clips[0].timeline_start == 0
        assert tracks[1].clips[0].timeline_start == 100

    def test_bus_topology_remapped(self):
        from dataclasses import replace
        # 子序列：ca1 → bus 子轨 cb1（send + 直连）
        cb = EditTrack(track_id="cb1", name="Bus", track_type="audio", index=1,
                       is_bus=True)
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", 0, 30)])
        routed = replace(
            child.audio_tracks[0],
            output_bus_id="cb1",
            sends=(Send(send_id="s1", target_bus_id="cb1", level=0.5),),
        )
        child = replace(child, audio_tracks=(routed, cb))
        asset = create_nested_sequence_asset(child)
        parent = _parent_with_nested(asset, _nested_clip(asset))
        project = EditProject.create(name="p")
        for a in (asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        tracks = flatten_nested_audio_tracks(parent, {"child1": child}.get,
                                             project.assets.get)
        by_id = {t.track_id: t for t in tracks}
        vt = by_id["nest:nest1:ca1"]
        vbus = by_id["nest:nest1:cb1"]
        assert vbus is not None and vbus.is_bus
        assert vt.output_bus_id == "nest:nest1:cb1"
        assert vt.sends[0].target_bus_id == "nest:nest1:cb1"

    def test_automation_keyframes_shifted(self):
        from dataclasses import replace
        kf_track = KeyframeTrack(
            param_name="volume",
            keyframes=[Keyframe(frame=5, value=0.5), Keyframe(frame=25, value=1.0)],
        )
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", 0, 30)])
        automated = replace(child.audio_tracks[0], automation={"volume": kf_track})
        child = replace(child, audio_tracks=(automated,))
        asset = create_nested_sequence_asset(child)
        parent = _parent_with_nested(asset, _nested_clip(asset, start=100))
        project = EditProject.create(name="p")
        for a in (asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        tracks = flatten_nested_audio_tracks(parent, {"child1": child}.get,
                                             project.assets.get)
        virtual = tracks[1]
        shifted = virtual.automation["volume"]
        assert [kf.frame for kf in shifted.keyframes] == [105, 125]
        # 原轨不被改动（共享安全）
        assert [kf.frame for kf in kf_track.keyframes] == [5, 25]

    def test_grandchild_recursion_and_cycle_guard(self):
        # 孙辈：child1 里再嵌 child2（音频在 child2）；child1 不含自身音频
        grand = _child_sequence("child2", clips=[
            _a_clip("gc1", "sfx", "ca1", 0, 20)])
        grand_asset = create_nested_sequence_asset(grand)
        gclip = ClipInstance(clip_id="gclip", asset_id=grand_asset.asset_id,
                             track_id="v1", timeline_start=5,
                             timeline_duration=20, source_start=0,
                             source_duration=20)
        gv = EditTrack(track_id="v1", name="V", track_type="video", index=0,
                       clips=(gclip,))
        child = TimelineSequence(timeline_id="child1", name="C",
                                 video_tracks=(gv,), audio_tracks=())
        child_asset = create_nested_sequence_asset(child)
        parent = _parent_with_nested(child_asset, _nested_clip(child_asset, start=1000))
        project = EditProject.create(name="p")
        for a in (child_asset, grand_asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        resolver = {"child1": child, "child2": grand}.get
        tracks = flatten_nested_audio_tracks(parent, resolver, project.assets.get)
        # 孙辈音频：5 (child1 坐标) + 1000 (父偏移) = 1005
        (vc,) = tracks[1].clips
        assert vc.timeline_start == 1005
        assert vc.asset_id == "sfx"

        # 环：让 child2 反过来引用 parent → 深度递归安全终止
        cyc_asset = create_nested_sequence_asset(
            TimelineSequence(timeline_id="child2", name="C2"))
        cyc = TimelineSequence(timeline_id="child2", name="C2")
        resolver_bad = {"child1": child, "child2": cyc, "parent": parent}.get
        # 正常返回（跳过环），不抛异常不递归爆栈
        flatten_nested_audio_tracks(parent, resolver_bad, project.assets.get)


# ─────────────────────────────────────────────────────────────────────────────
# 导出侧 helper
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestExportExpansionHelper:

    def test_no_nested_returns_same_sequence(self):
        from src.features.edit.core.timeline_export import (
            expand_sequence_audio_for_render,
        )
        seq = _child_sequence("plain", clips=[_a_clip("c", "sfx", "ca1", 0, 10)])
        project = EditProject.create(name="p")
        project = project.with_media_asset_added(_video_asset("sfx"))
        assert expand_sequence_audio_for_render(seq, None, project) is seq
        assert expand_sequence_audio_for_render(
            seq, {"x": seq}.get, project) is seq  # 无嵌套素材 → 原样

    def test_nested_expands_audio_tracks(self):
        from src.features.edit.core.timeline_export import (
            expand_sequence_audio_for_render,
        )
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", 10, 30)])
        asset = create_nested_sequence_asset(child)
        parent = _parent_with_nested(asset, _nested_clip(asset))
        project = EditProject.create(name="p")
        for a in (asset, _video_asset("sfx")):
            project = project.with_media_asset_added(a)
        expanded = expand_sequence_audio_for_render(
            parent, {"child1": child}.get, project)
        assert expanded is not parent
        assert len(expanded.audio_tracks) == 2
        assert expanded.audio_tracks[1].clips[0].timeline_start == 110
        # 视频轨未被触碰（展开只动 audio_tracks）
        assert expanded.video_tracks == parent.video_tracks


# ─────────────────────────────────────────────────────────────────────────────
# 实时侧：EditPage 音频同步（虚拟轨进引擎）
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestRealtimeSyncNestedAudio:

    @pytest.fixture()
    def no_real_audio(self, monkeypatch):
        from src.features.edit.ui.edit_page import EditPage as _EP
        fake_mgr = MagicMock()
        fake_mgr.get_all_track_ids.return_value = []
        fake_mgr.add_track.return_value = True
        monkeypatch.setattr(_EP, "_ensure_audio_manager", lambda self: fake_mgr)
        return fake_mgr

    def _nested_setup(self, tmp_path=None):
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "sfx", "ca1", 0, 30)])
        asset = create_nested_sequence_asset(child)
        parent = _parent_with_nested(asset, _nested_clip(asset))
        project = EditProject.create(name="p")
        # 实时路径有 Path(asset.path).exists() 检查——用真实临时文件
        if tmp_path is not None:
            real_file = tmp_path / "sfx.mp4"
            real_file.write_bytes(b"\x00" * 16)
            sfx = MediaAsset(
                asset_id="sfx", path=str(real_file), media_type="video",
                display_name="sfx", metadata={"fps": 24.0, "total_frames": 600},
            )
        else:
            sfx = _video_asset("sfx")
        for a in (asset, sfx):
            project = project.with_media_asset_added(a)
        return parent, project, {"child1": child}.get

    def test_virtual_track_registered_to_engine(self, qapp, no_real_audio, tmp_path):
        from src.features.edit.ui.edit_page import EditPage

        parent, project, resolver = self._nested_setup(tmp_path)
        page = EditPage()
        page._current_timeline = parent
        page._get_project = lambda: project
        page.set_audio_sequence_resolver(resolver)
        page._sync_audio_tracks_from_timeline()

        calls = no_real_audio.add_track.call_args_list
        registered_tracks = [c.args[0] if c.args else c.kwargs.get("track_id")
                             for c in calls]
        assert "nest:nest1:ca1" in registered_tracks
        nest_call = next(c for c in calls
                         if (c.args[0] if c.args else "") == "nest:nest1:ca1")
        kwargs = nest_call.kwargs
        assert kwargs.get("timeline_start") == 100
        assert kwargs.get("clip_id") == "nest:nest1:cc1"

    def test_without_resolver_behaves_as_before(self, qapp, no_real_audio):
        from src.features.edit.ui.edit_page import EditPage

        parent, project, _ = self._nested_setup()
        page = EditPage()
        page._current_timeline = parent
        page._get_project = lambda: project
        # 不注入 resolver：父轨无音频 → 音频不激活（旧行为）
        page._sync_audio_tracks_from_timeline()
        assert page._audio_active is False

    def test_retime_nested_audio_diagnostics_surfaced(self, qapp, no_real_audio, tmp_path):
        """F3.1：父 retime 嵌套音频跳过——预览不再静默（属性 + 信号）。"""
        from dataclasses import replace as _replace

        from src.features.edit.core.clip_params import SpeedParams
        from src.features.edit.ui.edit_page import EditPage

        parent, project, resolver = self._nested_setup(tmp_path)
        track = parent.video_tracks[0]
        nested = _replace(track.clips[0], speed=SpeedParams(speed=2.0))
        parent = _replace(parent, video_tracks=(_replace(track, clips=(nested,)),))

        page = EditPage()
        page._current_timeline = parent
        page._get_project = lambda: project
        page.set_audio_sequence_resolver(resolver)
        emitted = []
        page.signal_hub.nested_audio_diagnostics_changed.connect(
            lambda issues: emitted.append(tuple(issues)))
        page._sync_audio_tracks_from_timeline()

        codes = [d.code for d in page._last_nested_audio_diagnostics]
        assert "nested_audio_retime_unsupported" in codes
        assert emitted and any(
            d.code == "nested_audio_retime_unsupported" for d in emitted[-1])


# ─────────────────────────────────────────────────────────────────────────────
# 真实素材：展开 → 共享渲染器 → 真实解码 → 非静音 PCM
# ─────────────────────────────────────────────────────────────────────────────

_REAL_VIDEO = Path(__file__).resolve().parents[2] / "tests" / "test_videos" / "2K_h264.mp4"


@pytest.mark.real_media
@pytest.mark.skipif(not _REAL_VIDEO.exists(), reason="real test video missing")
@pytest.mark.edit_core
class TestRealMaterialNestedAudioMix:

    def test_flattened_clip_renders_real_pcm(self):
        """子序列音频（真实素材）经展开 + 共享渲染器产出非静音 PCM。"""
        from src.features.edit.core.stem_export import (
            StemSpec,
            StemType,
            render_stem_pcm,
        )

        real = MediaAsset(
            asset_id="real", path=str(_REAL_VIDEO), media_type="video",
            display_name="real",
            metadata={"fps": 24.0, "total_frames": 200},
        )
        # 子序列音频 clip：素材源 [100, 148)
        child = _child_sequence("child1", clips=[
            _a_clip("cc1", "real", "ca1", start=0, duration=48),
        ])
        # 修 source：直接用 asset 源 100 起
        from dataclasses import replace as _r
        child = _r(
            child,
            audio_tracks=(
                _r(child.audio_tracks[0], clips=(
                    _r(child.audio_tracks[0].clips[0],
                       source_start=100, source_duration=48),)),
            ),
        )
        asset = create_nested_sequence_asset(child)
        parent = _parent_with_nested(asset, _nested_clip(asset, start=24,
                                                         duration=48))
        project = EditProject.create(name="p")
        for a in (asset, real):
            project = project.with_media_asset_added(a)

        def decode_clip_fn(track, clip, frame_idx, fps, n_samples):
            """真实解码回调（对齐 timeline_export 的逐帧取源语义）。"""
            from src.features.edit.core.audio_playback import AudioDecoder

            local = frame_idx - clip.timeline_start
            if local < 0 or local >= clip.timeline_duration:
                return None
            dec = AudioDecoder()
            if not dec.open(Path(clip.asset.path if hasattr(clip, "asset") else _REAL_VIDEO),
                            timeline_fps=fps,
                            source_start=clip.source_start + local,
                            output_sample_rate=48000):
                return None
            try:
                samples = dec.decode_next_samples(n_samples)
                return samples
            finally:
                dec.close()

        pcm = render_stem_pcm(
            parent,
            StemSpec(stem_type=StemType.MASTER, name="M",
                     track_ids=(), include_master=True),
            sample_rate=48000,
            channels=2,
            decode_clip_fn=decode_clip_fn,
            sequence_resolver={"child1": child}.get,
            asset_lookup=project.assets.get,
        )
        assert pcm is not None
        # 展开的虚拟轨在 stem 里（master stem 覆盖全部轨）
        # 帧区间外是静音，窗口内（父 24..72 → PCM 对应段）应有实际能量
        mid = pcm.shape[1] // 2
        window = pcm[:, mid - 48000: mid + 48000] if pcm.shape[1] > 96000 else pcm
        assert float(np.abs(window).max()) > 0.001, (
            f"flattened nested audio is silent: max={float(np.abs(window).max())}")
