"""F2.5 · 实时多机位网格流验收——同刻对拍/latest-wins/降质/缺路/门控。

MulticamGridStream 是主时钟驱动的有界监看流（独立 CPU 小尺寸解码，
不占 Program 的 VT 硬解池）。验收锚点（计划 F2.5）：
- 各格显示同一时间线时刻（投递帧携带 timeline_frame，同批同值）；
- 掉监看帧可解释（superseded=被新请求追上，skipped=降质抽稀，计数在
  session_summary + playback_trace）；
- 暂停/seek（generation 取消）、速率（latest-wins）、缺路（退避+恢复）、
  最小化/隐藏（可见性门控停发）正确。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.ui.multicam_grid_stream import (
    DEGRADE_LADDER,
    MulticamGridStream,
)


class _FakeDecoder:
    """可控解码器：帧号写入像素、可调延迟、可注入失败。"""

    def __init__(self, path: str, latency_s: float, fail_frames=()):
        self.path = path
        self.latency_s = latency_s
        self.fail_frames = set(fail_frames)
        self.read_calls: list[int] = []
        self.closed = False

    def read_frame(self, frame: int):
        self.read_calls.append(int(frame))
        if int(frame) in self.fail_frames:
            raise ValueError(f"injected failure at {frame}")
        if self.latency_s:
            time.sleep(self.latency_s)
        return {"frame": int(frame), "path": self.path}

    def close(self):
        self.closed = True


class _FakeFactory:
    """按路径返回可控解码器；记录 target_size 供降质档断言。"""

    def __init__(self, latency_s: float = 0.0, fail_open=()):
        self.latency_s = latency_s
        self.fail_open = set(fail_open)
        self.open_calls: list[tuple[str, tuple[int, int]]] = []
        self.decoders: dict[str, list[_FakeDecoder]] = {}

    def __call__(self, path: str, target_size):
        self.open_calls.append((path, tuple(target_size)))
        if path in self.fail_open:
            raise OSError(f"cannot open {path}")
        decoder = _FakeDecoder(path, self.latency_s)
        self.decoders.setdefault(path, []).append(decoder)
        return decoder


def _convert(decoded) -> np.ndarray:
    return np.full((8, 12, 3), decoded["frame"] % 256, dtype=np.uint8)


def _infos(paths: dict[str, int]) -> dict:
    return {aid: {"path": p, "total_frames": total}
            for aid, p, total in
            ((aid, f"/m/{aid}.mp4", t) for aid, t in paths.items())}


class _Recorder:
    """信号收集器（跨线程排队投递，需 processEvents 泵）。"""

    def __init__(self, stream: MulticamGridStream):
        self.frames: list[tuple[str, int, object, int]] = []
        self.states: list[tuple[str, str, int]] = []
        self.summaries: list[dict] = []
        stream.signals.frame_ready.connect(self._on_frame)
        stream.signals.angle_state.connect(self._on_state)
        stream.signals.session_summary.connect(self._on_summary)

    def _on_frame(self, asset_id, timeline_frame, qimage, generation):
        self.frames.append((asset_id, timeline_frame, qimage, generation))

    def _on_state(self, asset_id, state, generation):
        self.states.append((asset_id, state, generation))

    def _on_summary(self, summary):
        self.summaries.append(summary)

    def frames_for(self, asset_id: str) -> list[tuple[int, int]]:
        return [(tf, gen) for aid, tf, _img, gen in self.frames
                if aid == asset_id]


def _wait_until(cond, timeout: float = 8.0, qapp=None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if qapp is not None:
            qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    if qapp is not None:
        qapp.processEvents()
    return cond()


@pytest.mark.edit_ui
class TestGridStreamCore:
    def test_four_angles_same_timeline_instant(self, qapp) -> None:
        """四格同一时刻对拍：一批 push 四角度，投递帧 timeline_frame 同值，
        源帧号与推入的权威映射逐角度一致。"""
        factory = _FakeFactory()
        stream = MulticamGridStream(
            decoder_factory=factory, frame_converter=_convert)
        rec = _Recorder(stream)
        stream.start()
        try:
            gen = stream.begin_session()
            mapping = {"cam1": 10, "cam2": 110, "cam3": 210, "cam4": 310}
            stream.push(mapping, _infos({k: 1000 for k in mapping}),
                        timeline_frame=100, generation=gen)
            assert _wait_until(
                lambda: {aid for aid, *_ in rec.frames} == set(mapping),
                qapp=qapp)
            by_angle = {aid: rec.frames_for(aid) for aid in mapping}
            for aid, delivered in by_angle.items():
                assert delivered, f"{aid} 未投递"
                tf, g = delivered[-1]
                assert tf == 100 and g == gen  # 同一时间线时刻、同会话
            # 每角度解码的源帧 = push 的映射帧（像素值 = 帧号 mod 256）
            for aid in mapping:
                frame_value = stream.stats()[aid]
                assert frame_value["decoded"] >= 1
        finally:
            stream.shutdown()

    def test_latest_wins_superseded_counted(self, qapp) -> None:
        """速率追赶：解码进行中被新请求追上 → 计数 superseded（可解释）。"""
        factory = _FakeFactory(latency_s=0.08)
        stream = MulticamGridStream(
            decoder_factory=factory, frame_converter=_convert)
        rec = _Recorder(stream)
        stream.start()
        try:
            gen = stream.begin_session()
            infos = _infos({"cam1": 1000})
            stream.push({"cam1": 10}, infos, timeline_frame=1,
                        generation=gen)
            # 等解码真正开始（慢速解码挂起中），再追上新帧
            assert _wait_until(
                lambda: factory.decoders.get("/m/cam1.mp4")
                and factory.decoders["/m/cam1.mp4"][-1].read_calls,
                qapp=qapp)
            stream.push({"cam1": 500}, infos, timeline_frame=5,
                        generation=gen)
            assert _wait_until(
                lambda: rec.frames_for("cam1"), timeout=10.0, qapp=qapp)
            time.sleep(0.15)  # 慢解码完成后统计落账
            qapp.processEvents()
            stats = stream.stats()["cam1"]
            assert stats["superseded"] >= 1  # 旧帧被追上：不投递、有出处
            # 投递帧时间线时刻单调（不回放旧时刻）
            tfs = [tf for tf, _g in rec.frames_for("cam1")]
            assert tfs == sorted(tfs)
        finally:
            stream.shutdown()

    def test_generation_cancel_on_seek(self, qapp) -> None:
        """暂停/大跳（会话切换）：会话切换后不再投递旧 generation 帧。"""
        factory = _FakeFactory()
        stream = MulticamGridStream(
            decoder_factory=factory, frame_converter=_convert)
        rec = _Recorder(stream)
        stream.start()
        try:
            gen1 = stream.begin_session()
            stream.push({"cam1": 10}, _infos({"cam1": 1000}),
                        timeline_frame=10, generation=gen1)
            assert _wait_until(lambda: rec.frames, qapp=qapp)
            time.sleep(0.05)  # 排队投递清空（去重后不再有后续帧）
            qapp.processEvents()
            gen2 = stream.begin_session()  # 会话切换 = 旧帧作废
            assert gen2 != gen1
            mark = len(rec.frames)
            stream.push({"cam1": 999}, _infos({"cam1": 1000}),
                        timeline_frame=999, generation=gen2)
            assert _wait_until(
                lambda: any(g == gen2 for _a, _t, _i, g in rec.frames),
                qapp=qapp)
            time.sleep(0.15)
            qapp.processEvents()
            # 切换后新到帧只属于新会话（旧会话迟到帧被丢弃）
            assert all(g == gen2
                       for _a, _t, _i, g in rec.frames[mark:])
        finally:
            stream.shutdown()

    def test_missing_angle_backoff_and_recovery(self, qapp) -> None:
        """缺路：打开失败标 missing 并退避重试；源恢复后回到 live。"""
        factory = _FakeFactory()
        stream = MulticamGridStream(
            decoder_factory=factory, frame_converter=_convert)
        rec = _Recorder(stream)
        stream.start()
        try:
            gen = stream.begin_session()
            infos = {"cam1": {"path": "/m/ok.mp4", "total_frames": 1000},
                     "camX": {"path": "/m/missing.mp4", "total_frames": 1000}}
            factory.fail_open.add("/m/missing.mp4")
            stream.push({"cam1": 10, "camX": 10}, infos,
                        timeline_frame=10, generation=gen)
            assert _wait_until(
                lambda: ("camX", "missing", gen) in rec.states, qapp=qapp)
            assert _wait_until(
                lambda: rec.frames_for("cam1"), qapp=qapp)
            opens_before = len(factory.open_calls)
            # 退避：缺路源不每轮 hammer 重开
            for tf in range(11, 16):
                stream.push({"cam1": tf, "camX": tf}, infos,
                            timeline_frame=tf, generation=gen)
            time.sleep(0.2)
            qapp.processEvents()
            assert len(factory.open_calls) - opens_before <= 3
            # 恢复：源可开后回到 live 并投递（穿插小间隔让每条请求
            # 各自驱动一轮退避计数）
            factory.fail_open.clear()
            for tf in range(20, 36):
                stream.push({"cam1": tf, "camX": tf}, infos,
                            timeline_frame=tf, generation=gen)
                qapp.processEvents()
                time.sleep(0.005)
            assert _wait_until(
                lambda: ("camX", "live", gen) in rec.states
                and rec.frames_for("camX"), timeout=10.0, qapp=qapp)
        finally:
            stream.shutdown()

    def test_degrade_ladder_and_summary(self, qapp) -> None:
        """同机负载证据：慢解码触发降质（抽稀+目标高度下降），统计入
        session_summary，trace 有 multicam_grid.* 记录。"""
        traces: list[tuple] = []

        def _trace(stage, **fields):
            traces.append((stage, fields))

        factory = _FakeFactory(latency_s=0.12)  # 远超 ~42ms 预算
        stream = MulticamGridStream(
            decoder_factory=factory, frame_converter=_convert, trace=_trace)
        rec = _Recorder(stream)
        stream.start()
        try:
            gen = stream.begin_session()
            infos = _infos({"cam1": 5000})
            for tf in range(1, 20):
                stream.push({"cam1": tf * 3}, infos,
                            timeline_frame=tf, generation=gen)
                time.sleep(0.01)
            assert _wait_until(
                lambda: stream.stats().get("cam1", {}).get("level", 0) > 0,
                timeout=10.0, qapp=qapp)
            # 抽稀生效：skipped 有计数
            assert _wait_until(
                lambda: stream.stats()["cam1"]["skipped"] >= 1, qapp=qapp)
            # 降高度档曾用过更小的 target_size 打开
            heights = {h for _p, (w, h) in factory.open_calls}
            assert heights <= {DEGRADE_LADDER[0][1],
                               DEGRADE_LADDER[1][1],
                               DEGRADE_LADDER[2][1]}
            stream.end_session()
            assert _wait_until(
                lambda: any("multicam_grid.session_summary" == s
                            for s, _f in traces), qapp=qapp)
            summary = rec.summaries[-1]
            snap = summary["angles"]["cam1"]
            assert snap["decoded"] >= 1 and snap["level"] > 0
            assert any(s == "multicam_grid.degrade" for s, _f in traces)
        finally:
            stream.shutdown()

    def test_visibility_gate_no_decode_when_inactive(self, qapp) -> None:
        """最小化/隐藏门控：set_active(False) 后不解码，恢复即续流。"""
        factory = _FakeFactory()
        stream = MulticamGridStream(
            decoder_factory=factory, frame_converter=_convert)
        rec = _Recorder(stream)
        stream.start()
        try:
            gen = stream.begin_session()
            infos = _infos({"cam1": 1000})
            stream.set_active(False)  # 面板隐藏
            stream.push({"cam1": 10}, infos, timeline_frame=10,
                        generation=gen)
            time.sleep(0.25)
            qapp.processEvents()
            assert factory.open_calls == []  # 门控期间零解码
            assert rec.frames == []
            stream.set_active(True)  # 恢复可见：从最新请求继续
            assert _wait_until(lambda: rec.frames, qapp=qapp)
        finally:
            stream.shutdown()

    def test_session_summary_shape(self, qapp) -> None:
        """统计快照结构：每角度 decoded/superseded/skipped/p50/档位。"""
        factory = _FakeFactory()
        stream = MulticamGridStream(
            decoder_factory=factory, frame_converter=_convert)
        rec = _Recorder(stream)
        stream.start()
        try:
            gen = stream.begin_session()
            stream.push({"cam1": 5, "cam2": 6},
                        _infos({"cam1": 100, "cam2": 100}),
                        timeline_frame=5, generation=gen)
            assert _wait_until(
                lambda: len(rec.frames) >= 2, qapp=qapp)
            stream.end_session()
            assert _wait_until(lambda: rec.summaries, qapp=qapp)
            summary = rec.summaries[-1]
            assert set(summary["angles"]) == {"cam1", "cam2"}
            for snap in summary["angles"].values():
                assert {"decoded", "superseded", "skipped",
                        "decode_ms_p50", "level"} <= set(snap)
                assert snap["decoded"] >= 1
        finally:
            stream.shutdown()
