"""批 A（AUD-18900 / AUD-19000 / AUD-12600 / AUD-12601）根因修复回归测试。

覆盖：
1. AUD-18900：``_GuardedPreviewSource`` acquire/retire 的 check-then-act
   由锁保护——在飞读者的源不会被关闭，close 恰好一次；
2. AUD-19000：TOPOS 直通 eligibility 拒绝带 ``animated_params`` 关键帧的
   clip（静态 opacity==1.0 / 恒等 transform 不再放行）；
3. AUD-12600：FCPXML frameDuration 按"每帧秒数"解析/写回（Apple 真实
   样例 ``100/2400s`` / ``1001/30000s``），帧数↔时间换算用 fps rational
   精确整数运算；
4. AUD-12601：``_frames_to_time`` 分母不再多乘 fps.den，NTSC 帧率的
   offset/duration 导出/回读逐帧精确。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.shared.media.metadata import Rational


# ================================================================
# AUD-18900 — _GuardedPreviewSource 原子 acquire/retire
# ================================================================


from src.features.edit.core.timeline_preview_renderer import (  # noqa: E402
    _GuardedPreviewSource,
)


class _CountingSource:
    """记录 close 次数的假 PyAV 源。"""

    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def test_guarded_source_retire_waits_for_inflight_reader() -> None:
    """在飞读者持源期间 retire 不得 close（延迟到最后一个 release）。"""
    src = _CountingSource()
    handle = _GuardedPreviewSource(src)
    assert handle.acquire() is src
    handle.retire()
    assert src.close_count == 0, "读者持源时关闭即 AUD-18900 段错误路径"
    handle.release()
    assert src.close_count == 1


def test_guarded_source_acquire_after_retire_returns_none() -> None:
    src = _CountingSource()
    handle = _GuardedPreviewSource(src)
    handle.retire()
    assert src.close_count == 1
    assert handle.acquire() is None
    assert src.close_count == 1


def test_guarded_source_close_is_idempotent() -> None:
    """重复 retire / retire+release 不双重 close PyAV 容器。"""
    src = _CountingSource()
    handle = _GuardedPreviewSource(src)
    handle.retire()
    handle.retire()
    assert src.close_count == 1

    src2 = _CountingSource()
    handle2 = _GuardedPreviewSource(src2)
    handle2.acquire()
    handle2.retire()      # 延迟
    handle2.retire()      # 幂等
    handle2.release()     # 最后一个读者 → close
    handle2.release()     # 多余 release 不得再 close
    assert src2.close_count == 1


def test_guarded_source_concurrent_acquire_retire_never_closes_inflight() -> None:
    """AUD-18900 竞态回归：并发 acquire/retire 下，acquire 成功即保证
    release 之前源未被 close（修复前 retire 可在 closed 检查与
    inflight += 1 之间立即 close）。"""
    for _ in range(300):
        src = _CountingSource()
        handle = _GuardedPreviewSource(src)
        violations: list[str] = []
        start = threading.Barrier(2)

        def reader() -> None:
            start.wait()
            acquired = handle.acquire()
            if acquired is not None:
                if src.close_count != 0:
                    violations.append("closed while reader in flight")
                handle.release()

        def closer() -> None:
            start.wait()
            handle.retire()

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=closer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert not violations
        # 无论谁先赢：句柄最终恰好关闭一次
        assert src.close_count == 1


# ================================================================
# AUD-19000 — 直通判定穷尽 animated_params
# ================================================================


from src.features.edit.core.clip_params import (  # noqa: E402
    CropParams,
    TransformParams,
)
from src.features.edit.core.timeline_render_plan import (  # noqa: E402
    build_direct_topos_delivery_plan,
)


class _StubClip:
    """与 tests/media/test_topos_direct_delivery.py 相同形状的 stub clip。

    默认不带 animated_params 属性（旧序列化数据/外部构造同样可能缺该
    属性），eligible 判定用 getattr 兜底。
    """

    def __init__(self, timeline_start: int = 0, duration: int = 48) -> None:
        self.clip_id = "c1"
        self.asset_id = "a1"
        self.is_enabled = True
        self.timeline_start = timeline_start
        self._duration = duration
        self.source_start = 0
        self.effects = ()
        self.color_stack_id = None
        self.composite_id = None
        self.transform = TransformParams()
        self.crop = CropParams()
        self.opacity = 1.0
        self.fade_in_duration = 0.0
        self.fade_out_duration = 0.0

    def effective_speed(self) -> float:
        return 1.0

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self._duration


class _StubTrack:
    def __init__(self, clips: tuple) -> None:
        self.clips = clips
        self.transitions = ()
        self.is_enabled = True


class _StubSequence:
    def __init__(self, tracks: tuple) -> None:
        self.video_tracks = tracks
        self.subtitle_tracks = ()


class _StubAsset:
    def __init__(self, path: str) -> None:
        self.media_type = "video"
        # 任意真实存在的文件即可——animated_params 拒绝发生在资产判定之前，
        # 而无动画的对照正例只需通过 isfile 检查。
        self.path = path
        self.metadata = {
            "codec": "topos",
            "pixel_format": "yuv422p10le",
            "bit_depth": 10,
            "width": 1920,
            "height": 1080,
            "fps_num": 25,
            "fps_den": 1,
            "total_frames": 96,
        }


class _StubProject:
    def __init__(self, asset: _StubAsset) -> None:
        self._asset = asset

    def get_asset_by_id(self, asset_id: str) -> _StubAsset:
        return self._asset


class _StubEncoderConfig:
    def __init__(self) -> None:
        self.codec = "topos"
        self.pix_fmt = "yuv422p10le"
        self.width = 1920
        self.height = 1080
        self.fps = 25.0
        self.has_alpha = False


class _StubExportConfig:
    def __init__(self) -> None:
        self.include_audio = False
        self.has_alpha = False


def _eligibility(clip: _StubClip):
    sequence = _StubSequence((_StubTrack((clip,)),))
    project = _StubProject(_StubAsset(str(Path(__file__))))
    return build_direct_topos_delivery_plan(
        sequence, project, _StubExportConfig(), _StubEncoderConfig(), 0, 48)


def test_animated_opacity_keyframes_reject_passthrough() -> None:
    """静态 opacity==1.0、恒等 transform，但带 opacity 关键帧动画的
    clip 必须被直通判定拒绝（渲染路径按关键帧逐帧求值）。"""
    clip = _StubClip()
    clip.animated_params = {"opacity": object()}  # 任意关键帧曲线对象
    plan, reason = _eligibility(clip)
    assert plan is None
    assert reason == "animated_params_present"


def test_animated_transform_keyframes_reject_passthrough() -> None:
    clip = _StubClip()
    clip.animated_params = {"transform.translate_x": object()}
    plan, reason = _eligibility(clip)
    assert plan is None
    assert reason == "animated_params_present"


def test_any_animated_param_rejects_passthrough() -> None:
    """穷尽语义：任何动画参数（不限于 opacity/transform）都拒绝直通。"""
    clip = _StubClip()
    clip.animated_params = {"volume": object()}
    plan, reason = _eligibility(clip)
    assert plan is None
    assert reason == "animated_params_present"


def test_empty_or_missing_animated_params_still_eligible() -> None:
    """无动画（属性缺失或空 dict）不误伤直通快路径。"""
    plan, reason = _eligibility(_StubClip())
    assert plan is not None, reason

    clip_empty = _StubClip()
    clip_empty.animated_params = {}
    plan, reason = _eligibility(clip_empty)
    assert plan is not None, reason


# ================================================================
# AUD-12600 / AUD-12601 — FCPXML frameDuration 与时间换算
# ================================================================


from src.features.edit.adapters.fcpxml_adapter import (  # noqa: E402
    FCPXMLAdapter,
    _fcpxml_frame_duration_to_fps,
    _frames_to_time,
    _rational_to_fcpxml_frame_duration,
    _time_to_frames,
)


class TestFrameDurationSemantics:
    """AUD-12600：frameDuration 是每帧秒数，与帧率互为倒数。"""

    def test_parse_apple_samples(self) -> None:
        # Apple 真实 FCPXML 常见写法（分子 100 = 1/100 秒刻度）
        assert _fcpxml_frame_duration_to_fps("100/2400s") == Rational(24, 1)
        assert _fcpxml_frame_duration_to_fps("100/2500s") == Rational(25, 1)
        assert _fcpxml_frame_duration_to_fps("100/3000s") == Rational(30, 1)
        # NTSC 精确有理数写法
        assert _fcpxml_frame_duration_to_fps(
            "1001/30000s") == Rational(30000, 1001)
        assert _fcpxml_frame_duration_to_fps(
            "1001/24000s") == Rational(24000, 1001)
        assert _fcpxml_frame_duration_to_fps(
            "1001/60000s") == Rational(60000, 1001)
        # 最简写法与纯整数
        assert _fcpxml_frame_duration_to_fps("1/24s") == Rational(24, 1)

    def test_write_frame_duration_is_per_frame_seconds(self) -> None:
        assert _rational_to_fcpxml_frame_duration(
            Rational(24, 1)) == "1/24s"
        assert _rational_to_fcpxml_frame_duration(
            Rational(30000, 1001)) == "1001/30000s"
        assert _rational_to_fcpxml_frame_duration(
            Rational(24000, 1001)) == "1001/24000s"

    def test_invalid_frame_duration_raises(self) -> None:
        with pytest.raises(ValueError):
            _fcpxml_frame_duration_to_fps("0/2400s")
        with pytest.raises(ValueError):
            _fcpxml_frame_duration_to_fps("100/0s")


class TestFramesTimeConversion:
    """AUD-12601：NTSC 帧率的时间换算精确（无 1001 倍缩小）。"""

    def test_frames_to_time_integer_fps(self) -> None:
        assert _frames_to_time(24, Rational(24, 1)) == "1/1s"
        assert _frames_to_time(120, Rational(24, 1)) == "5/1s"
        assert _frames_to_time(0, Rational(24, 1)) == "0s"

    def test_frames_to_time_ntsc_exact(self) -> None:
        # 3000 帧 @ 30000/1001 fps = 100.1 s = 1001/10 s
        # （旧实现输出 1001/30030000s，缩小约 1001 倍）
        assert _frames_to_time(3000, Rational(30000, 1001)) == "1001/10s"
        # 单帧时长 = 1001/30000 s，与 frameDuration 完全一致
        assert _frames_to_time(1, Rational(30000, 1001)) == "1001/30000s"
        assert _frames_to_time(1, Rational(24000, 1001)) == "1001/24000s"

    def test_time_to_frames_ntsc_exact(self) -> None:
        fps_ntsc = Rational(30000, 1001)
        assert _time_to_frames("1001/30000s", fps_ntsc) == 1
        assert _time_to_frames("1001/10s", fps_ntsc) == 3000
        # 29.97fps 下 10 秒 = 299.7 帧 → floor 299
        assert _time_to_frames("10/1s", fps_ntsc) == 299

    def test_time_to_frames_floor(self) -> None:
        assert _time_to_frames("1250/2400s", Rational(24, 1)) == 12

    @pytest.mark.parametrize("fps", [
        Rational(24, 1),
        Rational(25, 1),
        Rational(30, 1),
        Rational(24000, 1001),
        Rational(30000, 1001),
        Rational(60000, 1001),
    ])
    def test_frame_roundtrip_exact(self, fps: Rational) -> None:
        """frames → time → frames 在 NTSC/整数帧率下逐帧精确。"""
        for frames in (0, 1, 2, 12, 100, 299, 1001, 3000):
            assert _time_to_frames(
                _frames_to_time(frames, fps), fps) == frames, (
                f"roundtrip failed: fps={fps} frames={frames} "
                f"time={_frames_to_time(frames, fps)}")


# --- 真实 Apple 风格文件的导入导出 --- #


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "sample.fcpxml"
    p.write_text(content, encoding="utf-8")
    return p


def test_import_real_apple_ntsc_2997_timings(tmp_path: Path) -> None:
    """真实 FCP 导出的 NTSC 29.97 文件：时间必须按 30000/1001 fps 换算。"""
    doc = _write(tmp_path, """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="1920x1080" frameDuration="1001/30000s" width="1920" height="1080"/>
    <asset id="r2" name="clip_a" hasVideo="1" start="0s" duration="7200s">
      <media-rep kind="original" src="file:///home/clip_a.mov"/>
    </asset>
  </resources>
  <library><event name="E"><project name="P"><sequence id="seq1" name="Main"><spine>
    <asset-clip name="c0" ref="r2" offset="3003/30000s" duration="300300/30000s" tcStart="1001/30000s"/>
  </spine></sequence></project></event></library>
</fcpxml>
""")
    result = FCPXMLAdapter().import_to_canonical(doc)
    assert len(result.sequences) == 1
    seq = result.sequences[0]
    assert seq.fps == Rational(30000, 1001)
    clip = seq.video_tracks[0].clips[0]
    assert clip.timeline_start == 3        # 3003/30000 s × 30000/1001 fps
    assert clip.timeline_duration == 300   # 300300/30000 s
    assert clip.source_start == 1          # 1001/30000 s = 恰好一帧


def test_import_real_apple_24fps_timings(tmp_path: Path) -> None:
    doc = _write(tmp_path, """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="1920x1080" frameDuration="100/2400s" width="1920" height="1080"/>
    <asset id="r2" name="clip_a" hasVideo="1" start="0s" duration="7200s">
      <media-rep kind="original" src="file:///home/clip_a.mov"/>
    </asset>
  </resources>
  <library><event name="E"><project name="P"><sequence id="seq1" name="Main"><spine>
    <asset-clip name="c0" ref="r2" offset="1250/2400s" duration="1000/2400s" tcStart="500/2400s"/>
  </spine></sequence></project></event></library>
</fcpxml>
""")
    result = FCPXMLAdapter().import_to_canonical(doc)
    seq = result.sequences[0]
    assert seq.fps == Rational(24, 1)      # 100/2400 s/帧 → 24 fps
    clip = seq.video_tracks[0].clips[0]
    assert clip.timeline_start == 12       # floor(1250/100)
    assert clip.timeline_duration == 10
    assert clip.source_start == 5


def _ntsc_export_sequence():
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineMarker,
        TimelineSequence,
    )
    from src.features.edit.core.clip_params import (
        CropParams,
        SpeedParams,
        TransformParams,
    )
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=100, timeline_duration=240,
        source_start=50, source_duration=240,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    v1 = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="t1", name="NTSC Roundtrip", fps=Rational(30000, 1001),
        width=1920, height=1080, sample_rate=48000, video_tracks=(v1,),
        markers=(TimelineMarker(marker_id="m1", frame=60, name="beat"),),
    )


def test_export_ntsc_frame_duration_and_times(tmp_path: Path) -> None:
    out = tmp_path / "ntsc.fcpxml"
    FCPXMLAdapter().export_from_canonical(
        _ntsc_export_sequence(), options={"output_path": str(out)})
    root = ET.parse(str(out)).getroot()
    fmt = root.find(".//format")
    assert fmt is not None
    # 29.97fps → 每帧 1001/30000 秒（不再是秒级错误值 30000/1001s）
    assert fmt.get("frameDuration") == "1001/30000s"
    clips = root.findall(".//asset-clip")
    assert len(clips) == 1
    # 100 帧 @30000/1001 = 1001/300 s（旧实现输出 100100/30030000s）
    assert clips[0].get("offset") == "1001/300s"
    assert clips[0].get("duration") == "1001/125s"
    assert clips[0].get("tcStart") == "1001/600s"


def test_export_import_roundtrip_ntsc(tmp_path: Path) -> None:
    """Topos → FCPXML（NTSC 29.97）→ Topos：fps/placement/时长精确一致。"""
    out = tmp_path / "rt.fcpxml"
    adapter = FCPXMLAdapter()
    adapter.export_from_canonical(
        _ntsc_export_sequence(), options={"output_path": str(out)})
    result = adapter.import_to_canonical(out)
    assert len(result.sequences) == 1
    seq = result.sequences[0]
    assert seq.fps == Rational(30000, 1001)
    clip = seq.video_tracks[0].clips[0]
    assert clip.timeline_start == 100
    assert clip.timeline_duration == 240
    assert clip.source_start == 50
